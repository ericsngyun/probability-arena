"""MVP-005A edge_precheck_snapshots: probability-gap MEASUREMENT audit rows
(forecast probability vs market midpoint + validity checks). No dollar EV,
side, size, order, or execution columns exist — by design.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-04

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "edge_precheck_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market_ticker", sa.String(64), nullable=False),
        sa.Column(
            "signal_id", sa.Integer(), sa.ForeignKey("opportunity_signals.id"), nullable=True
        ),
        sa.Column(
            "forecast_id", sa.Integer(), sa.ForeignKey("market_forecasts.id"), nullable=False
        ),
        sa.Column(
            "market_snapshot_id",
            sa.Integer(),
            sa.ForeignKey("market_price_ticks.id"),
            nullable=True,
        ),
        sa.Column(
            "resolution_assessment_id",
            sa.Integer(),
            sa.ForeignKey("market_resolution_assessments.id"),
            nullable=True,
        ),
        sa.Column("forecaster_name", sa.String(64), nullable=False),
        sa.Column("evidence_depth", sa.String(16), nullable=False),
        sa.Column("forecast_probability", sa.Float(), nullable=False),
        sa.Column("forecast_confidence", sa.Float(), nullable=False),
        sa.Column("forecast_risk", sa.String(16), nullable=True),
        sa.Column("market_midpoint", sa.Float(), nullable=True),
        sa.Column("yes_bid", sa.Integer(), nullable=True),
        sa.Column("yes_ask", sa.Integer(), nullable=True),
        sa.Column("spread_cents", sa.Integer(), nullable=True),
        sa.Column("liquidity_proxy_cents", sa.Integer(), nullable=True),
        sa.Column("probability_gap", sa.Float(), nullable=True),
        sa.Column("abs_probability_gap", sa.Float(), nullable=True),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("invalidation_reasons", RAW_JSON, nullable=True),
        sa.Column("forecast_age_seconds", sa.Integer(), nullable=True),
        sa.Column("market_snapshot_age_seconds", sa.Integer(), nullable=True),
        sa.Column("persistence_count", sa.Integer(), nullable=False),
        sa.Column("thresholds", RAW_JSON, nullable=True),
        sa.Column("tags", RAW_JSON, nullable=True),
        sa.Column("raw_context", RAW_JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_edge_precheck_snapshots_market_ticker", "edge_precheck_snapshots", ["market_ticker"]
    )
    op.create_index(
        "ix_edge_precheck_snapshots_forecast_id", "edge_precheck_snapshots", ["forecast_id"]
    )
    op.create_index(
        "ix_edge_precheck_snapshots_forecaster_name",
        "edge_precheck_snapshots",
        ["forecaster_name"],
    )
    op.create_index("ix_edge_precheck_snapshots_status", "edge_precheck_snapshots", ["status"])


def downgrade() -> None:
    op.drop_table("edge_precheck_snapshots")
