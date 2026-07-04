"""SQLite backup/restore utilities (OPS-007).

`backup_database` takes a consistent snapshot via sqlite3's online backup
API (safe while the watcher/timers are writing), gzips it into
BACKUP_DIR/backup-<UTC timestamp>.db.gz, and prunes backups older than
BACKUP_RETENTION_DAYS. `verify_backup` decompresses to a temp file, runs
PRAGMA integrity_check, and confirms the expected core tables exist.

Non-SQLite databases are not backed up by this module — callers get a
guidance string (pg_dump) instead; nothing destructive is ever executed.
No secrets are read or printed (SQLite URLs carry no credentials here).
"""

import gzip
import logging
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.engine.url import make_url

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

BACKUP_PREFIX = "backup-"
BACKUP_SUFFIX = ".db.gz"

# A verified backup must contain at least these core data tables
# (alembic_version is reported but not required: create_all-based test DBs
# legitimately lack it)
EXPECTED_TABLES = frozenset(
    {"markets", "market_forecasts", "crypto_tokens", "marketops_runs"}
)

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


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    detail: str
    tables: int = 0


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


def list_backups(settings: Settings | None = None) -> list[Path]:
    """Existing backups, newest first."""
    settings = settings or get_settings()
    directory = _backup_dir(settings)
    if not directory.is_dir():
        return []
    return sorted(
        (p for p in directory.iterdir() if p.name.startswith(BACKUP_PREFIX)
         and p.name.endswith(BACKUP_SUFFIX)),
        key=lambda p: p.name,
        reverse=True,
    )


def prune_old_backups(settings: Settings | None = None) -> list[Path]:
    """Delete backups older than BACKUP_RETENTION_DAYS (by mtime).
    Returns the deleted paths."""
    settings = settings or get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.backup_retention_days)
    pruned: list[Path] = []
    for path in list_backups(settings):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            path.unlink()
            pruned.append(path)
            logger.info("Pruned old backup %s", path.name)
    return pruned


def backup_database(settings: Settings | None = None) -> BackupResult | str:
    """Consistent, compressed, timestamped SQLite backup; prunes old ones.
    Returns a guidance string (no action taken) for non-SQLite databases."""
    settings = settings or get_settings()
    db_path = _sqlite_path(settings)
    if db_path is None:
        return UNSUPPORTED_GUIDANCE
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found at {db_path}")

    directory = _backup_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        source = sqlite3.connect(str(db_path))
        try:
            snapshot = sqlite3.connect(str(tmp_path))
            try:
                source.backup(snapshot)  # online backup API: consistent under writers
            finally:
                snapshot.close()
        finally:
            source.close()
        with open(tmp_path, "rb") as raw, gzip.open(target, "wb") as compressed:
            shutil.copyfileobj(raw, compressed)
    finally:
        tmp_path.unlink(missing_ok=True)

    pruned = prune_old_backups(settings)
    return BackupResult(
        path=str(target),
        size_bytes=target.stat().st_size,
        pruned=[p.name for p in pruned],
    )


def verify_backup(path: str) -> VerifyResult:
    """Decompress to a temp file, run integrity_check, and confirm the
    expected core tables exist."""
    source = Path(path)
    if not source.exists():
        return VerifyResult(ok=False, detail=f"{path} does not exist")

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        try:
            with gzip.open(source, "rb") as compressed, open(tmp_path, "wb") as raw:
                shutil.copyfileobj(compressed, raw)
        except (OSError, gzip.BadGzipFile) as exc:
            return VerifyResult(ok=False, detail=f"not a readable gzip file: {exc}")
        try:
            conn = sqlite3.connect(str(tmp_path))
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    return VerifyResult(ok=False, detail=f"integrity_check: {integrity}")
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return VerifyResult(ok=False, detail=f"not a valid SQLite database: {exc}")
        missing = EXPECTED_TABLES - tables
        if missing:
            return VerifyResult(
                ok=False,
                detail=f"missing expected tables: {sorted(missing)}",
                tables=len(tables),
            )
        return VerifyResult(
            ok=True, detail=f"ok ({len(tables)} tables, integrity ok)", tables=len(tables)
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def backup_dir_stats(settings: Settings | None = None) -> tuple[int, float] | None:
    """(count, total MiB) of existing backups, or None when none exist."""
    backups = list_backups(settings)
    if not backups:
        return None
    total = sum(p.stat().st_size for p in backups)
    return len(backups), total / (1024 * 1024)
