from collections.abc import Iterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        legacy = inspector.has_table("markets") and not inspector.has_table("alembic_version")
    finally:
        engine.dispose()

    if legacy:
        command.stamp(config, "0001")
    command.upgrade(config, "head")
