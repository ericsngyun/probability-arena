"""OPS-013 tick-aggregation audit spine: tick_aggregation_runs.

One row per aggregation pass (manual or --scheduled): window, per-pass
counters, failed/oversized sub-windows, and status. This is the EVIDENCE the
raw-retention-readiness gates require — a retention reduction may only be
STAGED once scheduled runs are demonstrably clean over N cycles. Counters
only: no side, size, EV, dollar, profit, action, recommendation,
arbitrage/arb, order, wallet, signing, or execution column exists.

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "tick_aggregation_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("scheduled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("window_hours", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("subwindow_hours", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("bucket_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("rows_read", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("buckets_written", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("buckets_inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("buckets_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_windows", RAW_JSON),
        sa.Column("oversized_windows", RAW_JSON),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("error_type", sa.String(length=128)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("tick_aggregation_runs")
