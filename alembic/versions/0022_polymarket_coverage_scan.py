"""POLY-COVERAGE-001 scan-provenance columns on polymarket_scout_runs:
scan_mode, pages_fetched, market_fetch_errors, duplicates_dropped, queries_used.

These record HOW a bounded READ-ONLY coverage sample was obtained (which pages,
which Kalshi-derived search queries, how many provider errors and duplicates) so
the observer's audit spine explains its own coverage. Counters and provenance
only — no side, size, EV, dollar, profit, action, recommendation, arbitrage/arb
label, order, wallet, signing, or execution column exists here.

Additive and reversible: every column is nullable or server-defaulted, so an
existing polymarket_scout_runs row upgrades without backfill.

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-08

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

COLUMN_NAMES = (
    "scan_mode",
    "pages_fetched",
    "market_fetch_errors",
    "duplicates_dropped",
    "queries_used",
)


def _columns() -> tuple[sa.Column, ...]:
    """Build fresh Column objects per call — a Column instance may only ever be
    attached to one Table, so reusing module-level instances breaks an
    upgrade/downgrade/upgrade cycle inside a single process (as the migration
    round-trip tests do)."""
    return (
        sa.Column("scan_mode", sa.String(length=32), nullable=True),
        sa.Column("pages_fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("market_fetch_errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicates_dropped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("queries_used", RAW_JSON, nullable=True),
    )


def upgrade() -> None:
    with op.batch_alter_table("polymarket_scout_runs") as batch:
        for column in _columns():
            batch.add_column(column)


def downgrade() -> None:
    with op.batch_alter_table("polymarket_scout_runs") as batch:
        for name in reversed(COLUMN_NAMES):
            batch.drop_column(name)
