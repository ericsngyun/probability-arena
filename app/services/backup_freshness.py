"""SQLITE-BACKUP-FRESHNESS-ALERT-001 — local, manifest-backed backup health.

Answers exactly one question, from local files only:

    Does the canonical backup root contain a recent, committed, structurally
    valid, manifest-backed backup artifact produced within the last 36 hours?

`SQLITE-BACKUP-COORDINATION-001` publishes the DATA file first and the manifest
last, so a manifest is the *commit record* of a verified backup. This module
trusts that contract and re-checks only what can silently rot after publication:
the artifact still being there, still being a regular non-symlink file inside
the canonical root, still exactly the size the manifest committed to, still
carrying a gzip header, and the whole pair still being recent.

Deliberately NOT done here (see docs/SQLITE_BACKUP_FRESHNESS_ALERT_001.md §
"Full-hash policy"): no SHA-256 recomputation, no decompression, no SQLite
open. Those cost minutes on a 400+ MiB artifact and this runs on every
five-minute MarketOps cycle; full cryptographic verification stays in
`verify-db-backup`, which is the tool for "is this artifact byte-perfect?".

Read-only and provider-free: it never writes, never executes a backup, never
prunes, never modifies a backup file or manifest, and makes zero network calls.

Hostile-root posture. The backup root is mode 0700 on a SHARED host, so the
threat model is "someone or something can drop files in there". Every read is
therefore single-descriptor (`O_RDONLY|O_NOFOLLOW|O_NONBLOCK` + `fstat` on that
same fd), which closes the check/use race a `stat()`-then-`open()` pair leaves
open, refuses a symlink at open time, and makes a planted FIFO fail instead of
blocking the MarketOps cycle forever. A single unreadable or malicious file
must only ever invalidate ITS OWN candidate — never collapse the whole verdict
into `evaluation_error`, because a warning-severity "the monitor broke" is
exactly the outcome an attacker would want from a critical "the backups are
gone".
"""

import json
import logging
import os
import re
import stat as stat_module
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Settings, get_settings
from app.services.backup import (
    BACKUP_PREFIX,
    BACKUP_SUFFIX,
    MANIFEST_SUFFIX,
    MANIFEST_VERSION,
    STAMP_FORMAT,
    _backup_dir,
    _confined,
    _is_backup_name,
    _parse_stamp,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The freshness policy. ONE constant — the report CLI, the MarketOps evaluator,
# the alert path, the tests and the docs all read it from here so the threshold
# cannot drift between them. Deliberately a code constant, not a setting: a
# tunable "how stale is too stale" knob is exactly the kind of value that gets
# quietly widened until the alert stops meaning anything.
#
# Why 36h against the deployed daily timer (OnCalendar=*-*-* 01:30:00,
# RandomizedDelaySec=600, Persistent=true), measuring age from the manifest's
# committed created_at, which is the run's START:
#
#   * successful runs start 24h apart ± the 10 min jitter, so the worst-case
#     healthy gap is ~24h10m — and even a spring-forward on a local-time
#     OnCalendar only stretches that to ~25h10m. ~11h of headroom means normal
#     cadence, jitter, DST and a slow run can NEVER false-fire.
#   * a LATE run (retry, catch-up after downtime, a 25 min contended copy) is
#     tolerated for ~11h50m past its slot.
#
# It does NOT wait out a fully missed day, and that is intentional, not slack:
# after a miss the timer's own next attempt is ~48h after the last good backup,
# so 36h fires ~11.8h BEFORE that. The operator learns that last night's backup
# did not happen while there is still a day left to act, instead of after a
# second night has also gone by.
# ---------------------------------------------------------------------------
BACKUP_FRESHNESS_MAX_AGE_SECONDS = 36 * 60 * 60  # 129600

# Bounded inspection budget (this runs every MarketOps cycle).
MANIFEST_INSPECTION_CAP = 64      # newest N manifests READ; retention keeps <= 14
DIRECTORY_ENTRY_CAP = 4096        # entries scanned in the root before failing loudly
MAX_MANIFEST_BYTES = 64 * 1024    # a real manifest is ~1 KiB
MAX_MANIFEST_FIELD_CHARS = 64     # bound on any manifest string we echo back

# gzip magic; the artifact is always a gzip stream. Three bytes, not a decode.
GZIP_MAGIC = b"\x1f\x8b\x08"

# Accept every version up to the current publisher's. Pinning to
# {MANIFEST_VERSION} would mean that bumping the WRITER's version instantly
# marks the newest ON-DISK manifest unsupported — a self-inflicted critical
# alert lasting until the next nightly run, on a deploy that changed nothing
# about backup health. Readers accept a range; only writers pin.
SUPPORTED_MANIFEST_VERSIONS = frozenset(range(1, MANIFEST_VERSION + 1))
MANIFEST_STATUS_VERIFIED = "verified"
MANIFEST_INTEGRITY_OK = "ok"

# A manifest committed further than this into the future is not credible and
# must not be able to mask staleness. Sized for a real NTP/chrony step
# correction (VM suspend/resume, RTC drift after power loss) rather than for
# nothing at all: a backwards clock step must not page anyone about a backup
# that is seconds old and perfect.
MANIFEST_FUTURE_TOLERANCE_SECONDS = 300

# backup.py derives the filename stamp, `created_at` and `backup_filename` from
# ONE variable, so in genuine output all three always agree. Requiring that
# agreement costs two comparisons and removes the possibility of the entire
# freshness verdict resting on one mutable JSON scalar.
MANIFEST_STAMP_AGREEMENT_SECONDS = 1

# Shape only — never a pinned revision, which would drift on every migration.
# Permissive about punctuation on purpose: this repo numbers revisions
# 0001..0027, but Alembic allows arbitrary rev-ids and `--rev-id ops-001` is a
# common convention. `fullmatch` because `$` would also accept a trailing
# newline.
_ALEMBIC_REVISION_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")

# Deterministic, mutually exclusive health reasons. Unknown/malformed states are
# NEVER normalized into `backup_stale`: temporal staleness is only ever reported
# once the newest manifest/artifact pair is already structurally valid.
REASON_HEALTHY = "healthy"
REASON_ROOT_UNCONFIGURED = "backup_root_unconfigured"
REASON_ROOT_MISSING = "backup_root_missing"
REASON_ROOT_INVALID = "backup_root_invalid"
REASON_ROOT_SYMLINK = "backup_root_symlink"
REASON_ROOT_INSECURE = "backup_root_insecure"
REASON_INSPECTION_CAP = "backup_root_inspection_cap_exceeded"
REASON_NO_COMMITTED_BACKUP = "no_committed_backup"
REASON_MANIFEST_INVALID = "manifest_invalid"
REASON_MANIFEST_UNSUPPORTED = "manifest_unsupported"
REASON_MANIFEST_NOT_VERIFIED = "manifest_not_verified"
# Distinct from manifest_invalid on purpose: the manifest is intact and
# parseable, so "invalid" would send an operator hunting for corruption in a
# perfectly good file. backup.py treats alembic_revision as OPTIONAL (create_all
# test DBs legitimately lack it), so this reason means "not a backup of the
# migrated production database" — which, for THIS database, is still a fault.
REASON_MANIFEST_REVISION_INVALID = "manifest_revision_invalid"
# A clock problem, not an absent backup — deliberately not critical.
REASON_MANIFEST_FUTURE_DATED = "manifest_future_dated"
REASON_ARTIFACT_MISSING = "artifact_missing"
REASON_ARTIFACT_INVALID = "artifact_invalid"
REASON_ARTIFACT_SYMLINK = "artifact_symlink"
REASON_ARTIFACT_SIZE_MISMATCH = "artifact_size_mismatch"
REASON_BACKUP_STALE = "backup_stale"
REASON_EVALUATION_ERROR = "evaluation_error"

ALL_REASONS = (
    REASON_HEALTHY,
    REASON_ROOT_UNCONFIGURED,
    REASON_ROOT_MISSING,
    REASON_ROOT_INVALID,
    REASON_ROOT_SYMLINK,
    REASON_ROOT_INSECURE,
    REASON_INSPECTION_CAP,
    REASON_NO_COMMITTED_BACKUP,
    REASON_MANIFEST_INVALID,
    REASON_MANIFEST_UNSUPPORTED,
    REASON_MANIFEST_NOT_VERIFIED,
    REASON_MANIFEST_REVISION_INVALID,
    REASON_MANIFEST_FUTURE_DATED,
    REASON_ARTIFACT_MISSING,
    REASON_ARTIFACT_INVALID,
    REASON_ARTIFACT_SYMLINK,
    REASON_ARTIFACT_SIZE_MISMATCH,
    REASON_BACKUP_STALE,
    REASON_EVALUATION_ERROR,
)

STATUS_HEALTHY = "healthy"
STATUS_UNHEALTHY = "unhealthy"
STATUS_ERROR = "error"

# A broken CHECK and a clock step are not "backup protection is gone" — and
# paging on the monitor is how monitors get switched off. Everything else is.
WARNING_REASONS = frozenset({REASON_EVALUATION_ERROR, REASON_MANIFEST_FUTURE_DATED})
CRITICAL_REASONS = frozenset(ALL_REASONS) - WARNING_REASONS - {REASON_HEALTHY}

# Bounded, path-free, secret-free projection persisted into the MarketOps run
# summary and the alert evidence. Filenames are basenames only — never a path.
# Everything here exists to make ONE unhealthy alert triageable without SSH:
# both filenames (for a manifest-side fault `newest_backup_filename` is still
# None, so without `newest_manifest_filename` the alert names no file at all),
# both sizes (so a size mismatch says truncation-vs-append), and both manifest
# claims (so `manifest_not_verified` says WHICH of the two failed).
SUMMARY_FIELDS = (
    "status",
    "healthy",
    "reason",
    "newest_verified_at",
    "newest_manifest_filename",
    "newest_backup_filename",
    "age_seconds",
    "threshold_seconds",
    "artifact_exists",
    "artifact_bytes",
    "manifest_backup_bytes",
    "size_matches_manifest",
    "manifest_status",
    "manifest_integrity_check",
    "strict_manifest_count",
    "invalid_manifest_count",
    "external_calls",
    "duration_ms",
)


@dataclass(frozen=True)
class BackupFreshnessResult:
    """Closed result of one backup-health evaluation.

    Carries no database contents, no SQL rows, no provider payloads, no
    credentials, no environment contents, no arbitrary exception messages and
    no recomputed hashes — only bounded, structural facts about the backup root.
    """

    status: str
    healthy: bool
    reason: str
    evaluated_at: str
    threshold_seconds: int = BACKUP_FRESHNESS_MAX_AGE_SECONDS
    external_calls: int = 0
    duration_ms: int = 0

    # Backup root
    backup_root: str = ""            # operational config; excluded from summary_dict()
    backup_root_configured: bool = False
    backup_root_exists: bool = False
    backup_root_is_directory: bool = False
    backup_root_is_symlink: bool = False

    # Manifest inventory
    strict_manifest_count: int = 0
    invalid_manifest_count: int = 0
    newest_manifest_filename: str | None = None
    newest_backup_filename: str | None = None
    newest_verified_at: str | None = None
    age_seconds: float | None = None

    # Newest artifact
    artifact_exists: bool = False
    artifact_is_regular_file: bool = False
    artifact_is_symlink: bool = False
    artifact_bytes: int | None = None
    manifest_backup_bytes: int | None = None
    size_matches_manifest: bool = False

    # Newest manifest claims (bounded + sanitized before they are ever stored)
    manifest_version: int | None = None
    manifest_status: str | None = None
    manifest_integrity_check: str | None = None
    manifest_alembic_revision: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def summary_dict(self) -> dict:
        """Bounded projection for the MarketOps run summary and alert evidence.
        Excludes `backup_root` (a full path) and every unbounded manifest body
        field."""
        full = self.to_dict()
        return {key: full[key] for key in SUMMARY_FIELDS}

    @property
    def severity(self) -> str:
        """Repository alert severity for this state (Gate 9)."""
        if self.healthy:
            return "info"
        return "warning" if self.reason in WARNING_REASONS else "critical"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """Canonical repository policy (mirrors marketops._aware): a naive
    timestamp is interpreted as UTC, never as local time."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _bounded_text(value) -> str | None:
    """Manifest strings are attacker-influenced. Anything we echo back into a
    terminal, a JSON blob or an alert row is length-capped and stripped of
    control characters first — an `alembic_revision` of 40 KB containing raw
    ESC is terminal-control injection against whoever runs the report."""
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned[:MAX_MANIFEST_FIELD_CHARS]


def _canonical_stamp(text: str) -> datetime | None:
    """Parse a UTC stamp ONLY in its exact canonical form.

    `_parse_stamp` uses strptime, whose `%m`/`%d`/`%H` accept 1-2 digits, so
    `20260803T13426Z` parses — to 13:42:06, i.e. *newer* than the canonical
    01:34:26 of the same day. A stray file with such a name would win "newest",
    latch the alert at critical forever, and never be reaped (retention only
    prunes `.db.gz` names). Requiring the round-trip costs one strftime.
    """
    parsed = _parse_stamp(text)
    if parsed is None or parsed.strftime(STAMP_FORMAT) != text:
        return None
    return parsed


def _is_manifest_name(name: str) -> bool:
    """Strict match: `backup-<canonical UTC stamp>.manifest.json`. Anything else
    in the backup root — the lock file, working files, a `tmp/` directory, a
    foreign file — is not a manifest and is ignored, never inspected."""
    if not (name.startswith(BACKUP_PREFIX) and name.endswith(MANIFEST_SUFFIX)):
        return False
    return _canonical_stamp(name[len(BACKUP_PREFIX):-len(MANIFEST_SUFFIX)]) is not None


def _stamp_text_of(manifest_name: str) -> str:
    return manifest_name[len(BACKUP_PREFIX):-len(MANIFEST_SUFFIX)]


def _is_canonical_backup_name(name: str) -> bool:
    """`_is_backup_name` with the same canonical-form tightening."""
    if not (name.startswith(BACKUP_PREFIX) and name.endswith(BACKUP_SUFFIX)):
        return False
    if not _is_backup_name(name):
        return False
    return _canonical_stamp(name[len(BACKUP_PREFIX):-len(BACKUP_SUFFIX)]) is not None


def _parse_created_at(raw) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return _aware(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def _open_confined(path: Path):
    """Open a file inside the backup root for reading, once, safely.

    O_NOFOLLOW refuses a symlink at open time. O_NONBLOCK means a planted FIFO
    raises ENXIO instead of blocking the MarketOps cycle forever on a read that
    has no timeout anywhere. The caller then uses `os.fstat` on THIS descriptor,
    so the thing measured is provably the thing read.
    """
    return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)


def _read_manifest_bytes(path: Path) -> bytes | None:
    """Bounded single-descriptor manifest read; None when it is not a regular
    file, is too large, or cannot be read."""
    try:
        fd = _open_confined(path)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode):
            return None
        if info.st_size > MAX_MANIFEST_BYTES:
            return None
        data = os.read(fd, MAX_MANIFEST_BYTES + 1)
        return None if len(data) > MAX_MANIFEST_BYTES else data
    except OSError:
        return None
    finally:
        os.close(fd)


@dataclass(frozen=True)
class _Candidate:
    """One strict-named manifest file plus whatever we could read from it."""

    filename: str
    filename_stamp: datetime
    payload: dict | None          # None => unreadable / unparseable / not an object
    created_at: datetime | None   # None => absent or unparseable

    @property
    def order_key(self) -> tuple[datetime, str]:
        """Newest-first ordering key.

        Primary: the manifest's own committed `created_at`. A manifest we could
        NOT parse still competes using its (strict, always-present) filename
        stamp — that is what stops a corrupt newest manifest from being silently
        skipped in favour of an older healthy one. Deterministic filename
        tie-break.
        """
        return (self.created_at or self.filename_stamp, self.filename)

    @property
    def is_invalid(self) -> bool:
        return self.payload is None or self.created_at is None


def _read_candidate(path: Path, root: Path, stamp: datetime) -> _Candidate:
    """Read one strict-named manifest.

    NEVER raises. An entry that is not a confined regular file (symlink,
    traversal escape, directory, FIFO), is oversized, or whose JSON cannot be
    parsed — including pathological input such as deeply nested arrays, which
    raise RecursionError rather than ValueError — becomes an INVALID candidate.
    It is never opened unsafely and never followed, but it still competes for
    "newest", so a planted or corrupted newest manifest surfaces as unhealthy
    instead of quietly handing the verdict to an older backup, and one bad file
    can never collapse the whole evaluation into `evaluation_error`.
    """
    invalid = _Candidate(path.name, stamp, None, None)
    try:
        if not _confined(path, root):
            return invalid
        data = _read_manifest_bytes(path)
        if data is None:
            return invalid
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict):
            return invalid
        return _Candidate(
            path.name, stamp, payload, _parse_created_at(payload.get("created_at"))
        )
    except Exception:
        return invalid


def _scan_manifests(root: Path) -> tuple[list[_Candidate], int, bool]:
    """(candidates read, total strict manifests present, directory cap exceeded).

    ONE non-recursive scan of the canonical root. Never descends into
    subdirectories and never follows symlinks.

    Names are collected first, sorted newest-first, and only the newest
    MANIFEST_INSPECTION_CAP are READ. That ordering matters: reading in
    arbitrary `scandir` order and then bailing when the cap is hit would turn a
    large-but-perfectly-healthy backup inventory into a critical "backup
    protection is gone" page. Sorting first makes the read cap unable to change
    the verdict at all — the newest manifest is always among those inspected —
    while `strict_manifest_count` still reports the true total. Lexicographic
    order on the fixed-width canonical stamp IS chronological order.
    """
    names: list[str] = []
    entries = 0
    with os.scandir(root) as iterator:
        for entry in iterator:
            entries += 1
            if entries > DIRECTORY_ENTRY_CAP:
                # A root with this many entries is itself broken; fail loudly
                # rather than inspect an arbitrary truncated slice of it.
                return [], len(names), True
            if _is_manifest_name(entry.name):
                names.append(entry.name)

    names.sort(reverse=True)
    candidates = []
    for name in names[:MANIFEST_INSPECTION_CAP]:
        stamp = _canonical_stamp(_stamp_text_of(name))
        if stamp is None:  # pragma: no cover - _is_manifest_name already parsed it
            continue
        candidates.append(_read_candidate(root / name, root, stamp))
    return candidates, len(names), False


def _artifact_facts(path: Path) -> tuple[int, bool] | None:
    """(size_bytes, gzip_magic_ok) via ONE descriptor, or None when the entry is
    not a readable regular file. Reads 3 bytes — never decompresses, never
    hashes, never opens the backup as a database."""
    try:
        fd = _open_confined(path)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode):
            return None
        head = os.read(fd, len(GZIP_MAGIC))
        return info.st_size, head == GZIP_MAGIC
    except OSError:
        return None
    finally:
        os.close(fd)


def evaluate_backup_freshness(
    settings: Settings | None = None,
    *,
    backup_root: Path | str | None = None,
    now: datetime | None = None,
) -> BackupFreshnessResult:
    """Evaluate local backup health. Read-only, provider-free, bounded.

    `backup_root` / `now` are dependency-injection seams for tests; production
    callers (the MarketOps hook and the report CLI) always pass neither, so the
    canonical `BACKUP_DIR` and the real clock are used. There is deliberately no
    CLI flag for either — see the docs; a read-only tool that walks an arbitrary
    operator-supplied path is a strictly larger surface than this check needs.
    """
    started = _now()
    now = _aware(now) if now is not None else started
    evaluated_at = now.isoformat()
    base: dict = {}

    def finish(reason: str, **overrides) -> BackupFreshnessResult:
        healthy = reason == REASON_HEALTHY
        # Explicit precedence rather than relying on base/overrides being
        # disjoint: a future reordering that collides would otherwise raise
        # TypeError and be silently downgraded to `evaluation_error`.
        fields = {**base, **overrides}
        return BackupFreshnessResult(
            status=(
                STATUS_HEALTHY if healthy
                else STATUS_ERROR if reason == REASON_EVALUATION_ERROR
                else STATUS_UNHEALTHY
            ),
            healthy=healthy,
            reason=reason,
            evaluated_at=evaluated_at,
            duration_ms=max(0, int((_now() - started).total_seconds() * 1000)),
            **fields,
        )

    try:
        settings = settings or get_settings()
        if backup_root is not None:
            root = Path(backup_root)
            base["backup_root_configured"] = True
        else:
            configured = bool((settings.backup_dir or "").strip())
            base["backup_root_configured"] = configured
            if not configured:
                return finish(REASON_ROOT_UNCONFIGURED)
            root = _backup_dir(settings)

        base["backup_root"] = str(root)

        # --- backup root ---------------------------------------------------
        if root.is_symlink():
            # Checked before exists(): a symlink to a real directory passes both
            # exists() and is_dir(), and a swappable root is not a canonical root.
            return finish(REASON_ROOT_SYMLINK, backup_root_is_symlink=True)
        if not root.exists():
            return finish(REASON_ROOT_MISSING)
        base["backup_root_exists"] = True
        if not root.is_dir():
            return finish(REASON_ROOT_INVALID)
        base["backup_root_is_directory"] = True

        # backup.py creates the root 0700 but nothing ever re-verifies it. A
        # group/world-WRITABLE backup root means anyone can forge the very
        # manifests this check trusts. (Ownership is deliberately not checked:
        # legitimate deployments can run under a different uid than the owner,
        # and a false critical on the monitor is its own failure mode.)
        if root.stat().st_mode & 0o022:
            return finish(REASON_ROOT_INSECURE)

        # --- manifest inventory --------------------------------------------
        candidates, strict_total, dir_cap_exceeded = _scan_manifests(root)
        base["strict_manifest_count"] = strict_total
        base["invalid_manifest_count"] = sum(1 for c in candidates if c.is_invalid)
        if dir_cap_exceeded:
            return finish(REASON_INSPECTION_CAP)
        if not candidates:
            return finish(REASON_NO_COMMITTED_BACKUP)

        # --- newest committed manifest, no silent fallback -------------------
        newest = max(candidates, key=lambda c: c.order_key)
        base["newest_manifest_filename"] = newest.filename

        # Future-dating is checked on the FILENAME stamp too, before anything
        # else. An unparseable candidate orders by its filename stamp, so a
        # single stray `backup-20991231T000000Z.manifest.json` would otherwise
        # win "newest" forever and latch a CRITICAL alert over a perfectly
        # healthy backup — and retention would never reap it, because retention
        # only ever prunes `.db.gz` names.
        if (now - newest.filename_stamp).total_seconds() \
                < -MANIFEST_FUTURE_TOLERANCE_SECONDS:
            return finish(REASON_MANIFEST_FUTURE_DATED)

        if newest.payload is None or newest.created_at is None:
            return finish(REASON_MANIFEST_INVALID)

        payload = newest.payload
        version = payload.get("manifest_version")
        version_ok = isinstance(version, int) and not isinstance(version, bool)
        base["manifest_version"] = version if version_ok else None
        if not version_ok:
            return finish(REASON_MANIFEST_INVALID)
        if version not in SUPPORTED_MANIFEST_VERSIONS:
            return finish(REASON_MANIFEST_UNSUPPORTED)

        base["manifest_status"] = _bounded_text(payload.get("status"))
        base["manifest_integrity_check"] = _bounded_text(payload.get("integrity_check"))
        if payload.get("status") != MANIFEST_STATUS_VERIFIED:
            return finish(REASON_MANIFEST_NOT_VERIFIED)
        if payload.get("integrity_check") != MANIFEST_INTEGRITY_OK:
            # A "verified" manifest that does not record a clean integrity_check
            # is self-contradictory: treat it as not a verified backup.
            return finish(REASON_MANIFEST_NOT_VERIFIED)

        revision = payload.get("alembic_revision")
        base["manifest_alembic_revision"] = _bounded_text(revision)
        if not isinstance(revision, str) or not _ALEMBIC_REVISION_RE.fullmatch(revision):
            return finish(REASON_MANIFEST_REVISION_INVALID)

        # `created_at` must agree with the stamp the publisher derived it from.
        # Without this the whole freshness verdict rests on one mutable JSON
        # scalar, with an immutable corroborating value sitting unused in the
        # filename right next to it.
        if abs((newest.created_at - newest.filename_stamp).total_seconds()) \
                > MANIFEST_STAMP_AGREEMENT_SECONDS:
            return finish(REASON_MANIFEST_INVALID)

        base["newest_verified_at"] = newest.created_at.isoformat()
        # Rounded ONCE, then both reported and compared, so a verdict can never
        # disagree with the number printed next to it.
        age_seconds = round((now - newest.created_at).total_seconds(), 3)

        if age_seconds < -MANIFEST_FUTURE_TOLERANCE_SECONDS:
            return finish(REASON_MANIFEST_FUTURE_DATED, age_seconds=age_seconds)
        base["age_seconds"] = age_seconds

        backup_bytes = payload.get("backup_bytes")
        if not isinstance(backup_bytes, int) or isinstance(backup_bytes, bool) \
                or backup_bytes < 0:
            return finish(REASON_MANIFEST_INVALID)
        base["manifest_backup_bytes"] = backup_bytes

        # The manifest may only certify ITS OWN artifact — same stamp, strict
        # canonical name, no separators. Otherwise a fresh manifest could vouch
        # for an artifact from an entirely different run.
        filename = payload.get("backup_filename")
        expected = f"{BACKUP_PREFIX}{_stamp_text_of(newest.filename)}{BACKUP_SUFFIX}"
        if not isinstance(filename, str) or filename != Path(filename).name \
                or not _is_canonical_backup_name(filename) or filename != expected:
            return finish(REASON_MANIFEST_INVALID)
        base["newest_backup_filename"] = filename

        # --- artifact --------------------------------------------------------
        artifact = root / filename
        if artifact.is_symlink():
            return finish(
                REASON_ARTIFACT_SYMLINK, artifact_exists=True, artifact_is_symlink=True
            )
        if not artifact.exists():
            return finish(REASON_ARTIFACT_MISSING)
        base["artifact_exists"] = True
        if not _confined(artifact, root):
            # Escapes the canonical root (traversal / bind trickery).
            return finish(REASON_ARTIFACT_INVALID)

        facts = _artifact_facts(artifact)
        if facts is None:
            return finish(REASON_ARTIFACT_INVALID)
        artifact_bytes, magic_ok = facts
        base["artifact_is_regular_file"] = True
        base["artifact_bytes"] = artifact_bytes
        if artifact_bytes != backup_bytes:
            return finish(REASON_ARTIFACT_SIZE_MISMATCH)
        base["size_matches_manifest"] = True
        if not magic_ok:
            # Cheapest possible content check. It does NOT make `healthy` mean
            # "restorable" — see the docs — but it catches an artifact replaced
            # wholesale by something that is not a gzip stream at all.
            return finish(REASON_ARTIFACT_INVALID)

        # --- freshness (only after the pair is structurally valid) -----------
        if age_seconds > BACKUP_FRESHNESS_MAX_AGE_SECONDS:
            return finish(REASON_BACKUP_STALE)
        return finish(REASON_HEALTHY)

    except Exception:  # never propagate: this check can only ever report
        logger.warning("backup freshness evaluation failed", exc_info=True)
        # No exception text is carried into the result — an arbitrary message
        # is exactly the field that leaks paths and environment detail.
        base.pop("backup_root", None)
        return finish(REASON_EVALUATION_ERROR)


def freshness_deadline(from_time: datetime) -> datetime:
    """The instant at which a backup committed at `from_time` becomes stale."""
    return _aware(from_time) + timedelta(seconds=BACKUP_FRESHNESS_MAX_AGE_SECONDS)
