"""SQLite backup/restore utilities (OPS-007, hardened by SQLITE-BACKUP-COORDINATION-001).

`backup_database` takes a consistent snapshot via sqlite3's online backup API
(safe while the watcher/timers are writing) and publishes it only after the
artifact verifies:

    capacity gate -> exclusive backup lock -> online-backup snapshot -> gzip to
    <target>.part -> verify (integrity_check + required tables + sha256) ->
    manifest -> atomic os.replace publication -> bounded tiered retention

Nothing is published until verification passes, and retention never deletes the
newest backup. Backups are compressed to BACKUP_DIR/backup-<UTC timestamp>.db.gz
with a sibling backup-<UTC timestamp>.manifest.json.

Non-SQLite databases are not backed up by this module — callers get a guidance
string (pg_dump) instead; nothing destructive is ever executed. No secrets are
read or printed (SQLite URLs carry no credentials here), and the manifest
records a redacted source path only.
"""

import fcntl
import gzip
import hashlib
import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine.url import make_url

from app.config import Settings, get_settings
from app.telemetry.sqlite_events import OpContext, emit_event

logger = logging.getLogger(__name__)


def _emit_backup_event(
    ctx: OpContext | None, *, outcome: str, exc: BaseException | None = None,
    db_path: Path | None = None, journal_mode: str | None = None,
    synchronous_mode: str | None = None,
) -> None:
    """SQLITE-LOCK-TELEMETRY-001A: one reader op event per real backup, for
    writer-overlap measurement. Emitted AFTER the backup finishes or after
    terminal failure handling; never raises, never changes backup behavior."""
    if ctx is None:
        return
    try:
        gauges: dict = {}
        try:
            if db_path is not None and db_path.exists():
                gauges["database_bytes"] = db_path.stat().st_size
                vfs = os.statvfs(db_path.parent)
                gauges["filesystem_free_bytes"] = vfs.f_bavail * vfs.f_frsize
        except OSError:  # gauge loss must not drop the whole event
            gauges = {}
        category = None
        if exc is not None:
            if isinstance(exc, FileNotFoundError):
                category = "filesystem_error"
            elif isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
                category = "disk_full"
            elif isinstance(exc, OSError):
                category = "filesystem_error"
            else:
                category = "unknown"
        emit_event(
            ctx,
            outcome=outcome,
            exception_class=type(exc).__name__ if exc is not None else None,
            exception_category=category,
            journal_mode=journal_mode,
            synchronous_mode=synchronous_mode,
            external_calls=0,
            filesystem_io_during_transaction=False,  # reader; no write txn
            **gauges,
        )
    except Exception:  # pragma: no cover - defensive
        pass

BACKUP_PREFIX = "backup-"
BACKUP_SUFFIX = ".db.gz"
MANIFEST_SUFFIX = ".manifest.json"
PARTIAL_SUFFIX = ".part"
LOCK_FILENAME = ".backup.lock"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"

MANIFEST_VERSION = 1
DIR_MODE = 0o700
FILE_MODE = 0o600

# Capacity: the uncompressed snapshot and its gzip coexist in the backup dir,
# and verification decompresses a third full copy into TMPDIR (which we pin to
# the backup filesystem), so require 3x the database plus a bounded margin.
CAPACITY_MULTIPLIER = 3
CAPACITY_MARGIN_BYTES = 512 * 1024 * 1024  # 512 MiB

# Writer coordination (SQLITE-BACKUP-COORDINATION-001).
#
# The production database runs journal_mode=delete with a 30 s busy timeout. A
# single-step online backup (sqlite3's default pages=-1) would hold one SHARED
# read lock for the WHOLE multi-GiB copy; in rollback-journal mode that blocks
# every writer's COMMIT, and a multi-minute hold exceeds the 30 s busy handler,
# turning recoverable waits into hard "database is locked" failures.
#
# Copying in bounded steps releases and reacquires the read lock between steps,
# so writers interleave normally. The cost is that a write from another
# connection restarts the copy; RESTART/DEADLINE guards below keep that bounded
# instead of spinning forever on a busy database.
BACKUP_PAGES_PER_STEP = 4096  # ~16 MiB at a 4 KiB page size
BACKUP_STEP_SLEEP_SECONDS = 0.05
BACKUP_MAX_RESTARTS = 12
BACKUP_DEADLINE_SECONDS = 1500  # 25 min hard stop, well inside TimeoutStartSec

# Compression: level 9 measured ~14 MiB/s with no meaningful ratio gain over
# level 6; level 1 is ~2x faster for ~4% more size. On a multi-GiB database the
# critical path matters more than the last few percent.
COMPRESS_LEVEL = 6

# Bounded tiered retention. The newest backup is never deleted.
RETAIN_DAILY = 7
RETAIN_WEEKLY = 4
RETAIN_MONTHLY = 3

# A verified backup must contain at least these core data tables
# (alembic_version is reported but not required: create_all-based test DBs
# legitimately lack it)
EXPECTED_TABLES = frozenset(
    {"markets", "market_forecasts", "crypto_tokens", "marketops_runs"}
)

# Bounded, non-row-revealing counts recorded in the manifest.
MANIFEST_COUNT_TABLES = ("markets", "market_forecasts", "marketops_runs")

UNSUPPORTED_GUIDANCE = (
    "backup-db supports SQLite only. For Postgres use pg_dump manually, e.g.: "
    "pg_dump --format=custom --file=backup.dump <database-url> "
    "(run it yourself — this tool never executes it)."
)


@dataclass(frozen=True)
class BackupResult:
    path: str
    size_bytes: int
    pruned: list[str]
    status: str = "success"
    detail: str = ""
    manifest_path: str = ""
    sha256: str = ""
    duration_ms: int = 0
    verification_ms: int = 0


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    detail: str
    tables: int = 0
    sha256: str = ""
    alembic_revision: str | None = None
    counts: dict = field(default_factory=dict)


class BackupContentionError(RuntimeError):
    """The online copy could not converge against concurrent writers within the
    bounded restart/deadline budget. Not a corruption signal — the source and
    every existing backup are untouched."""


@dataclass(frozen=True)
class RetentionPlan:
    retained: list[str]
    deleted: list[str]
    skipped: list[str]
    dry_run: bool


def _sqlite_path(settings: Settings) -> Path | None:
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        return None
    return Path(url.database)


def _backup_dir(settings: Settings) -> Path:
    directory = Path(settings.backup_dir)
    if not directory.is_absolute():
        db_path = _sqlite_path(settings)
        # anchor relative backup dirs next to the project data, not the CWD
        anchor = db_path.parent.parent if db_path is not None else Path.cwd()
        directory = anchor / directory
    return directory


def _ensure_backup_dir(directory: Path) -> Path:
    """Create (or tighten) the backup root to 0700. Never follows a symlink."""
    if directory.is_symlink():
        raise ValueError(f"backup directory must not be a symlink: {directory}")
    directory.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    try:
        os.chmod(directory, DIR_MODE)
    except OSError:  # pragma: no cover - best effort on odd filesystems
        pass
    return directory


def _parse_stamp(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _is_backup_name(name: str) -> bool:
    """Strict match: prefix + parseable UTC stamp + suffix. Nothing else is
    ever a deletion candidate."""
    if not (name.startswith(BACKUP_PREFIX) and name.endswith(BACKUP_SUFFIX)):
        return False
    return _parse_stamp(name[len(BACKUP_PREFIX):-len(BACKUP_SUFFIX)]) is not None


def _stamp_of(path: Path) -> datetime | None:
    name = path.name
    if not (name.startswith(BACKUP_PREFIX) and name.endswith(BACKUP_SUFFIX)):
        return None
    return _parse_stamp(name[len(BACKUP_PREFIX):-len(BACKUP_SUFFIX)])


def _confined(path: Path, root: Path) -> bool:
    """True only when `path` resolves to a direct child of `root` and is not a
    symlink. Blocks traversal and symlink escapes."""
    if path.is_symlink():
        return False
    try:
        return path.resolve(strict=False).parent == root.resolve(strict=False)
    except OSError:  # pragma: no cover - defensive
        return False


def free_bytes(directory: Path) -> int:
    vfs = os.statvfs(directory)
    return vfs.f_bavail * vfs.f_frsize


def capacity_required(db_bytes: int) -> int:
    return CAPACITY_MULTIPLIER * db_bytes + CAPACITY_MARGIN_BYTES


@contextmanager
def _backup_lock(directory: Path):
    """Bounded, non-blocking, dedicated backup lock.

    flock on a coordination-only file — it never touches the database's own
    locking. The kernel releases it when the process dies, so a crashed run
    cannot leave a stale lock (no TOCTOU PID check, no unbounded wait, no
    retry loop). Yields True when acquired, False when another run holds it.
    """
    lock_path = directory / LOCK_FILENAME
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, FILE_MODE)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - defensive
                pass
    finally:
        os.close(fd)


def _create_private(path: Path):
    """Create `path` O_EXCL|O_NOFOLLOW at 0600 and return the fd.

    Writing to a bare path and chmod'ing afterwards leaves two holes: a planted
    symlink at the (predictable, second-granular) name would be followed and its
    target truncated, and the file exists 0644 until the chmod lands — which for
    the raw snapshot means a full plaintext copy of the database is briefly
    group/world readable. O_EXCL also makes a same-second stamp collision fail
    loudly instead of silently overwriting an already-published backup.
    """
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                   FILE_MODE)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _application_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            timeout=10, cwd=str(Path(__file__).resolve().parents[2]),
        )
        return (out.stdout.strip() or None) if out.returncode == 0 else None
    except Exception:  # pragma: no cover - git absence must not fail a backup
        return None


def list_backups(settings: Settings | None = None) -> list[Path]:
    """Existing backups, newest first."""
    settings = settings or get_settings()
    directory = _backup_dir(settings)
    if not directory.is_dir():
        return []
    return sorted(
        (p for p in directory.iterdir()
         if _is_backup_name(p.name) and _confined(p, directory) and p.is_file()),
        key=lambda p: p.name,
        reverse=True,
    )


def plan_retention(
    settings: Settings | None = None, *, now: datetime | None = None
) -> RetentionPlan:
    """Bounded tiered retention: newest RETAIN_DAILY days, RETAIN_WEEKLY ISO
    weeks, RETAIN_MONTHLY months. The newest backup is ALWAYS retained.
    Anything that is not a strictly-named, confined, regular backup file is
    skipped — never deleted."""
    settings = settings or get_settings()
    directory = _backup_dir(settings)
    if not directory.is_dir():
        return RetentionPlan([], [], [], True)

    candidates: list[tuple[datetime, Path]] = []
    skipped: list[str] = []
    for entry in sorted(directory.iterdir(), key=lambda p: p.name, reverse=True):
        if entry.name == LOCK_FILENAME:
            continue
        if not _is_backup_name(entry.name):
            skipped.append(entry.name)  # manifests, partials, foreign files
            continue
        if not _confined(entry, directory) or not entry.is_file():
            skipped.append(entry.name)  # symlink / traversal / non-regular
            continue
        stamp = _stamp_of(entry)
        if stamp is None:  # pragma: no cover - _is_backup_name already parsed it
            skipped.append(entry.name)
            continue
        candidates.append((stamp, entry))

    candidates.sort(key=lambda item: item[0], reverse=True)
    keep: set[Path] = set()
    # Recency floor: the newest RETAIN_DAILY backups are always kept, whatever
    # day they fall on. Without this a same-day manual backup taken before a
    # risky operation would be pruned by that night's scheduled run.
    for _stamp, path in candidates[:RETAIN_DAILY]:
        keep.add(path)

    seen_days: list[str] = []
    seen_weeks: list[str] = []
    seen_months: list[str] = []
    for stamp, path in candidates:
        day = stamp.strftime("%Y-%m-%d")
        iso = stamp.isocalendar()
        week = f"{iso[0]}-W{iso[1]:02d}"
        month = stamp.strftime("%Y-%m")
        if day not in seen_days:
            if len(seen_days) < RETAIN_DAILY:
                keep.add(path)
            seen_days.append(day)
        if week not in seen_weeks:
            if len(seen_weeks) < RETAIN_WEEKLY:
                keep.add(path)
            seen_weeks.append(week)
        if month not in seen_months:
            if len(seen_months) < RETAIN_MONTHLY:
                keep.add(path)
            seen_months.append(month)

    retained = [p.name for _, p in candidates if p in keep]
    deleted = [p.name for _, p in candidates if p not in keep]
    return RetentionPlan(retained, deleted, skipped, True)


def apply_retention(
    settings: Settings | None = None, *, dry_run: bool = True,
    now: datetime | None = None,
) -> RetentionPlan:
    """Evaluate retention; delete only when dry_run is False. Deletion is
    confined to the backup root, strict-name-matched, symlink-rejecting, and
    always leaves the newest backup in place. A backup's manifest is removed
    with it."""
    settings = settings or get_settings()
    if dry_run:
        return plan_retention(settings)
    directory = _backup_dir(settings)
    if directory.is_symlink():
        # backup_database refuses a symlinked root; the destructive path must too
        raise ValueError(f"backup directory must not be a symlink: {directory}")
    plan = plan_retention(settings)
    for name in plan.deleted:
        target = directory / name
        if not _is_backup_name(name) or not _confined(target, directory):
            continue  # defence in depth: never delete anything unexpected
        try:
            target.unlink()
            logger.info("Pruned old backup %s", name)
            manifest = directory / (name[:-len(BACKUP_SUFFIX)] + MANIFEST_SUFFIX)
            if _confined(manifest, directory) and manifest.is_file():
                manifest.unlink()
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("Could not prune %s: %s", name, exc)
    return RetentionPlan(plan.retained, plan.deleted, plan.skipped, False)


def prune_old_backups(settings: Settings | None = None) -> list[Path]:
    """Backwards-compatible wrapper (OPS-007 API): applies the bounded tiered
    retention policy and returns the deleted paths."""
    settings = settings or get_settings()
    directory = _backup_dir(settings)
    plan = apply_retention(settings, dry_run=False)
    return [directory / name for name in plan.deleted]


def _inspect_sqlite(path: Path) -> tuple[str, set[str], str | None, dict]:
    """(integrity, tables, alembic_revision, counts) from a plain .db file,
    opened read-only. Never migrates."""
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        revision = None
        if "alembic_version" in tables:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            revision = row[0] if row else None
        counts: dict = {}
        for table in MANIFEST_COUNT_TABLES:
            if table in tables:
                # fixed module-level allowlist; never user input
                counts[table] = conn.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0]
        return integrity, tables, revision, counts
    finally:
        conn.close()


def verify_backup(path: str, *, tmp_dir: Path | None = None) -> VerifyResult:
    """Decompress to a temp file, run integrity_check, and confirm the
    expected core tables exist. Also reports sha256, Alembic revision, and
    bounded table counts. Read-only; never migrates the backup."""
    source = Path(path)
    if source.is_symlink():
        return VerifyResult(ok=False, detail=f"{path} is a symlink")
    if not source.exists():
        return VerifyResult(ok=False, detail=f"{path} does not exist")
    if source.stat().st_size == 0:
        return VerifyResult(ok=False, detail=f"{path} is empty")

    with tempfile.NamedTemporaryFile(
        suffix=".db", delete=False, dir=str(tmp_dir) if tmp_dir else None
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        try:
            with gzip.open(source, "rb") as compressed, open(tmp_path, "wb") as raw:
                shutil.copyfileobj(compressed, raw)
        except (OSError, gzip.BadGzipFile, EOFError) as exc:
            return VerifyResult(ok=False, detail=f"not a readable gzip file: {exc}")
        if tmp_path.stat().st_size == 0:
            return VerifyResult(ok=False, detail="decompressed backup is empty")
        with open(tmp_path, "rb") as handle:
            if handle.read(16) != b"SQLite format 3\x00":
                return VerifyResult(ok=False, detail="invalid SQLite header")
        try:
            integrity, tables, revision, counts = _inspect_sqlite(tmp_path)
        except sqlite3.Error as exc:
            return VerifyResult(ok=False, detail=f"not a valid SQLite database: {exc}")
        if integrity != "ok":
            return VerifyResult(ok=False, detail=f"integrity_check: {integrity}")
        missing = EXPECTED_TABLES - tables
        if missing:
            return VerifyResult(
                ok=False,
                detail=f"missing expected tables: {sorted(missing)}",
                tables=len(tables),
            )
        return VerifyResult(
            ok=True,
            detail=f"ok ({len(tables)} tables, integrity ok)",
            tables=len(tables),
            sha256=_sha256(source),
            alembic_revision=revision,
            counts=counts,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def _reap_stale_partials(directory: Path, *, older_than_seconds: int = 6 * 3600) -> list[str]:
    """Remove leftover working files from a run that was killed before its
    cleanup could execute (e.g. SIGTERM on a systemd TimeoutStartSec overrun,
    which does not unwind Python's handlers).

    Only ever called while the exclusive backup flock is held, so nothing can be
    in flight. Confined to the backup root, matches only our own working-file
    shapes, and never touches a published artifact or manifest.
    """
    reaped: list[str] = []
    if not directory.is_dir():
        return reaped
    cutoff = time.time() - older_than_seconds
    for entry in directory.iterdir():
        name = entry.name
        is_working = name.endswith(PARTIAL_SUFFIX) or (
            name.startswith(f".{BACKUP_PREFIX}") and name.endswith(f".raw{PARTIAL_SUFFIX}")
        )
        if not is_working or not _confined(entry, directory) or not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                reaped.append(name)
                logger.warning("Reaped stale backup working file %s", name)
        except OSError:  # pragma: no cover - defensive
            continue
    return reaped


def backup_database(
    settings: Settings | None = None, *, dry_run: bool = False
) -> BackupResult | str:
    """Consistent, verified, compressed, timestamped SQLite backup.

    Publishes atomically only after the artifact verifies, then applies the
    bounded tiered retention policy. Returns a guidance string (no action
    taken) for non-SQLite databases. Overlap and capacity shortfalls return a
    bounded skipped result rather than raising, so a skipped backup can never
    fail its caller (e.g. a systemd oneshot) in an unbounded way.
    """
    settings = settings or get_settings()
    db_path = _sqlite_path(settings)
    if db_path is None:
        return UNSUPPORTED_GUIDANCE  # no operation performed — no event

    try:
        ctx = OpContext(
            writer_name="backup", writer_class="maintenance",
            operation_name="backup_database",
        )
    except Exception:  # pragma: no cover - defensive
        ctx = None
    journal_mode = synchronous_mode = None

    if not db_path.exists():
        # OPS-007 parity: raise exactly as before, after emitting a failed event.
        missing = FileNotFoundError(f"SQLite database not found at {db_path}")
        _emit_backup_event(ctx, outcome="failed_other", exc=missing, db_path=db_path)
        raise missing

    directory = _ensure_backup_dir(_backup_dir(settings))
    db_bytes = db_path.stat().st_size
    required = capacity_required(db_bytes)
    available = free_bytes(directory)

    if dry_run:
        plan = plan_retention(settings)
        return BackupResult(
            path="", size_bytes=0, pruned=plan.deleted, status="dry_run",
            detail=(
                f"would back up {db_bytes} B; destination {directory} has "
                f"{available} B free, requires {required} B; "
                f"capacity_ok={available >= required}; "
                f"retention would delete {len(plan.deleted)}"
            ),
        )

    if available < required:
        # Fail closed BEFORE writing. Deletes nothing, never retries.
        return BackupResult(
            path="", size_bytes=0, pruned=[], status="skipped_capacity",
            detail=(
                f"insufficient space at {directory}: {available} B free, "
                f"requires {required} B (2x{db_bytes} + margin)"
            ),
        )

    with _backup_lock(directory) as acquired:
        if not acquired:
            return BackupResult(
                path="", size_bytes=0, pruned=[], status="skipped_overlap",
                detail="another backup run holds the backup lock",
            )

        _reap_stale_partials(directory)

        started = datetime.now(timezone.utc)
        stamp = started.strftime(STAMP_FORMAT)
        target = directory / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
        partial = directory / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}{PARTIAL_SUFFIX}"
        manifest_target = directory / f"{BACKUP_PREFIX}{stamp}{MANIFEST_SUFFIX}"
        manifest_partial = Path(str(manifest_target) + PARTIAL_SUFFIX)
        # Snapshot beside the destination (same filesystem) so the uncompressed
        # copy never lands on an unrelated, possibly pressured volume.
        raw_path = directory / f".{BACKUP_PREFIX}{stamp}.raw{PARTIAL_SUFFIX}"

        try:
            source = sqlite3.connect(str(db_path))
            try:
                try:
                    # query form only — sampling, never an assignment
                    journal_mode = source.execute(
                        "PRAGMA journal_mode").fetchone()[0]
                    synchronous_mode = str(source.execute(
                        "PRAGMA synchronous").fetchone()[0])
                except Exception:  # pragma: no cover - sampling is optional
                    journal_mode = synchronous_mode = None
                os.close(_create_private(raw_path))
                snapshot = sqlite3.connect(str(raw_path))
                try:
                    # Bounded, restart-aware chunked online backup: never holds
                    # one long read lock against journal_mode=delete writers.
                    state = {"remaining": None, "restarts": 0}
                    deadline = time.monotonic() + BACKUP_DEADLINE_SECONDS

                    def _progress(_status, remaining, _total):
                        previous = state["remaining"]
                        if previous is not None and remaining > previous:
                            state["restarts"] += 1
                            if state["restarts"] > BACKUP_MAX_RESTARTS:
                                raise BackupContentionError(
                                    f"online copy restarted "
                                    f"{state['restarts']} times under concurrent "
                                    f"writes; giving up without blocking them"
                                )
                        state["remaining"] = remaining
                        if time.monotonic() > deadline:
                            raise BackupContentionError(
                                f"online copy exceeded "
                                f"{BACKUP_DEADLINE_SECONDS}s deadline"
                            )

                    source.backup(
                        snapshot,
                        pages=BACKUP_PAGES_PER_STEP,
                        sleep=BACKUP_STEP_SLEEP_SECONDS,
                        progress=_progress,
                    )
                    restarts = state["restarts"]
                finally:
                    snapshot.close()
            finally:
                source.close()
            with open(raw_path, "rb") as raw, os.fdopen(
                _create_private(partial), "wb"
            ) as gz_fd, gzip.GzipFile(
                fileobj=gz_fd, mode="wb", compresslevel=COMPRESS_LEVEL
            ) as compressed:
                shutil.copyfileobj(raw, compressed)
            raw_path.unlink(missing_ok=True)

            backup_ms = int(
                (datetime.now(timezone.utc) - started).total_seconds() * 1000)

            # Verify the PARTIAL artifact. Nothing is published unless this passes.
            verify_started = datetime.now(timezone.utc)
            verdict = verify_backup(str(partial), tmp_dir=directory)
            verify_ms = int(
                (datetime.now(timezone.utc) - verify_started).total_seconds() * 1000)
            if not verdict.ok:
                partial.unlink(missing_ok=True)
                _emit_backup_event(
                    ctx, outcome="failed_other", db_path=db_path,
                    journal_mode=journal_mode, synchronous_mode=synchronous_mode,
                )
                return BackupResult(
                    path="", size_bytes=0, pruned=[], status="failed_verification",
                    detail=f"verification failed, nothing published: {verdict.detail}",
                    duration_ms=backup_ms, verification_ms=verify_ms,
                )

            manifest = {
                "manifest_version": MANIFEST_VERSION,
                "created_at": started.isoformat(),
                "source_database_path_redacted": f".../{db_path.name}",
                "source_database_bytes": db_bytes,
                "backup_filename": target.name,
                "backup_bytes": partial.stat().st_size,
                "sha256": verdict.sha256,
                "alembic_revision": verdict.alembic_revision,
                "sqlite_version": sqlite3.sqlite_version,
                "journal_mode": journal_mode,
                "integrity_check": "ok",
                "required_tables_present": sorted(EXPECTED_TABLES),
                "selected_table_counts": verdict.counts,
                "backup_duration_ms": backup_ms,
                "online_copy_restarts": restarts,
                "verification_duration_ms": verify_ms,
                "host": socket.gethostname(),
                "application_commit": _application_commit(),
                "status": "verified",
            }
            with os.fdopen(_create_private(manifest_partial), "w") as handle:
                handle.write(json.dumps(manifest, indent=2, sort_keys=True))

            # Atomic publication (same directory => same filesystem).
            os.replace(manifest_partial, manifest_target)
            os.replace(partial, target)
        except BackupContentionError as exc:
            for leftover in (raw_path, partial, manifest_partial):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:  # pragma: no cover - defensive
                    pass
            _emit_backup_event(
                ctx, outcome="skipped_contention", exc=exc, db_path=db_path,
                journal_mode=journal_mode, synchronous_mode=synchronous_mode,
            )
            return BackupResult(
                path="", size_bytes=0, pruned=[], status="skipped_contention",
                detail=str(exc),
            )
        except BaseException as exc:
            for leftover in (raw_path, partial, manifest_partial):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:  # pragma: no cover - defensive
                    pass
            _emit_backup_event(
                ctx, outcome="failed_other", exc=exc, db_path=db_path,
                journal_mode=journal_mode, synchronous_mode=synchronous_mode,
            )
            raise

        # Retention runs only after a verified publication.
        plan = apply_retention(settings, dry_run=False)

    _emit_backup_event(
        ctx, outcome="success", db_path=db_path,
        journal_mode=journal_mode, synchronous_mode=synchronous_mode,
    )
    return BackupResult(
        path=str(target),
        size_bytes=target.stat().st_size,
        pruned=plan.deleted,
        status="success",
        detail=verdict.detail,
        manifest_path=str(manifest_target),
        sha256=verdict.sha256,
        duration_ms=backup_ms,
        verification_ms=verify_ms,
    )


def backup_dir_stats(settings: Settings | None = None) -> tuple[int, float] | None:
    """(count, total MiB) of existing backups, or None when none exist."""
    backups = list_backups(settings)
    if not backups:
        return None
    total = sum(p.stat().st_size for p in backups)
    return len(backups), total / (1024 * 1024)
