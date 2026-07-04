"""MarketOps Autopilot (OPS-006) coordination-audit tables: marketops_runs,
marketops_alerts. Read-only orchestration telemetry — no EV, trade, order,
wallet, or execution columns exist.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-04

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "marketops_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("config", RAW_JSON, nullable=True),
        sa.Column("summary", RAW_JSON, nullable=True),
        sa.Column("signals_seen", sa.Integer(), nullable=False),
        sa.Column("signals_promoted", sa.Integer(), nullable=False),
        sa.Column("signals_processed", sa.Integer(), nullable=False),
        sa.Column("crypto_tokens_seen", sa.Integer(), nullable=False),
        sa.Column("crypto_signals_created", sa.Integer(), nullable=False),
        sa.Column("outcomes_synced", sa.Integer(), nullable=False),
        sa.Column("forecasts_scored", sa.Integer(), nullable=False),
        sa.Column("alerts_created", sa.Integer(), nullable=False),
        sa.Column("error_type", sa.String(128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "marketops_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("alert_type", sa.String(48), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("evidence", RAW_JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_marketops_alerts_alert_type", "marketops_alerts", ["alert_type"])
    op.create_index("ix_marketops_alerts_status", "marketops_alerts", ["status"])


def downgrade() -> None:
    op.drop_table("marketops_alerts")
    op.drop_table("marketops_runs")
