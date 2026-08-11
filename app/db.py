from collections.abc import Iterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MigrationRequiredError(RuntimeError):
    """CRYPTO-COVERAGE-REPAIR-001 B10 — raised by `ensure_schema_current`
    when `migration_mode="guarded"` (the safe default) finds the database
    behind the code's required Alembic revision. Ordinary runtime never
    auto-upgrades in this mode; see `ensure_schema_current`'s docstring for
    the required operator sequence."""


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None


def connect_args_for(url: str) -> dict:
    """SQLite gets a busy timeout (seconds) so concurrent writers wait for
    the lock instead of failing immediately (OPS-007 — the marketops timer
    and a manual CLI run share one file). Other backends get no extra args."""
    if url.startswith("sqlite"):
        return {"timeout": get_settings().sqlite_busy_timeout_ms / 1000}
    return {}


def get_engine():
    global _engine
    if _engine is None:
        url = get_settings().database_url
        _engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args_for(url))
    return _engine


def get_sessionmaker() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a scoped session."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def run_migrations(database_url: str | None = None) -> None:
    """Bring the database to the latest Alembic revision.

    Databases created by MVP-001's create_all (tables exist, no alembic_version)
    are stamped at revision 0001 first, then upgraded normally.
    """
    url = database_url or get_settings().database_url
    config = Config()
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)

    # CRYPTO-COVERAGE-REPAIR-001 B11 — this engine previously had no
    # `connect_args`, so this inspection query (and, before the matching
    # alembic/env.py fix, the actual migration `command.upgrade` call) ran at
    # Python's incidental 5s `sqlite3` default rather than this app's
    # declared `sqlite_busy_timeout_ms` (30s). Same busy policy as every
    # other SQLite writer in this codebase — see `connect_args_for`.
    engine = create_engine(url, connect_args=connect_args_for(url))
    try:
        inspector = inspect(engine)
        legacy = inspector.has_table("markets") and not inspector.has_table("alembic_version")
    finally:
        engine.dispose()

    if legacy:
        command.stamp(config, "0001")
    command.upgrade(config, "head")


def _alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _head_revision(config: Config) -> str | None:
    return ScriptDirectory.from_config(config).get_current_head()


def _current_revision(url: str) -> str | None:
    """The revision actually stamped in this database's `alembic_version`
    table, or `None` if the table doesn't exist (a brand-new database, or a
    legacy pre-Alembic `create_all` database with no `alembic_version` row
    at all)."""
    engine = create_engine(url, connect_args=connect_args_for(url))
    try:
        inspector = inspect(engine)
        if not inspector.has_table("alembic_version"):
            return None
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).fetchone()
            return row[0] if row else None
    finally:
        engine.dispose()


def ensure_schema_current(database_url: str | None = None) -> None:
    """CRYPTO-COVERAGE-REPAIR-001 B10 — the migration-governance gate every
    ordinary runtime call site (roughly 100 in `app/cli.py`, plus
    `app/main.py`'s FastAPI startup) calls INSTEAD OF `run_migrations`
    directly.

    `migration_mode="guarded"` (the default, and the required production
    setting) never applies a migration here. It only CHECKS: if the
    database's stamped Alembic revision is not exactly the code's head
    revision — including a brand-new/legacy database with no
    `alembic_version` row at all — it raises `MigrationRequiredError` with
    the exact operator sequence to run, and does nothing else. This is what
    makes a `git pull` deployment safe against the 5-minute MarketOps timer
    (and every other recurring/one-shot runtime caller): the timer's next
    tick fails loudly and instantly instead of silently applying Alembic
    ahead of a backup or an operator's own upgrade step.

    `migration_mode="auto"` delegates to `run_migrations` (the old
    always-upgrade behaviour) — convenient for local development and
    first-install bootstrapping, but must be set deliberately; it is never
    inferred from a hostname, a path, or any other brittle signal. Any value
    other than exactly "auto" is treated as guarded, so a typo in the env
    var fails closed rather than silently opening the safe default.
    """
    settings = get_settings()
    url = database_url or settings.database_url

    if settings.migration_mode == "auto":
        run_migrations(url)
        return

    config = _alembic_config(url)
    head = _head_revision(config)
    current = _current_revision(url)

    if current == head:
        return

    raise MigrationRequiredError(
        "MIGRATION_REQUIRED: database schema revision "
        f"{current!r} does not match the code's required head revision "
        f"{head!r}. Runtime does not auto-upgrade in migration_mode="
        f"{settings.migration_mode!r} (the safe default) — this is expected "
        "and correct after a code deploy that added a migration. To "
        "upgrade: "
        "1) confirm a fresh backup exists — "
        "`python -m app.cli sqlite-backup-freshness-report`; "
        "2) apply the migration explicitly — `alembic upgrade head` (from "
        "the repo root, with the deploy target's DATABASE_URL); "
        "3) verify integrity and revision — `python -m app.cli "
        "agent-context` should show `alembic revision` equal to the head "
        "revision above, with no errors; "
        "4) only then restart or resume runtime (the timer/service/CLI "
        "call that raised this error). "
        "To allow automatic upgrade in local development or test, set "
        "MIGRATION_MODE=auto explicitly — never on a production host."
    )
