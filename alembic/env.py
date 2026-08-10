import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import models  # noqa: F401  (register mappings on Base.metadata)
from app.config import get_settings
from app.db import Base, connect_args_for

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    # Programmatic callers (app.db.run_migrations, tests) set sqlalchemy.url
    # explicitly; the alembic CLI falls back to app settings / DATABASE_URL.
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # CRYPTO-COVERAGE-REPAIR-001 B11 — before this fix, this engine had no
    # `connect_args`, so SQLite migrations ran at Python's incidental 5s
    # default `sqlite3` lock timeout instead of the app's deliberately
    # reviewed `sqlite_busy_timeout_ms` (30s — see app/db.py:connect_args_for
    # and app/config.py:sqlite_busy_timeout_ms). A migration that hit a real
    # writer lock therefore had a DIFFERENT, undeclared busy policy than the
    # runtime app: it could fail 6x faster than any other SQLite writer in
    # this codebase, with no stated reason. Reuse the exact same policy the
    # app itself uses, so "a migration under a blocking lock" and "an app
    # writer under a blocking lock" mean the same declared wait, not two
    # silently different ones.
    connectable = create_engine(
        get_url(), poolclass=pool.NullPool, connect_args=connect_args_for(get_url()),
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER columns in place; batch mode recreates tables
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
