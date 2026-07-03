"""Real-time watcher tables: market_price_ticks, opportunity_signals,
watcher_runs.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-03

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "market_price_ticks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market_ticker", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("yes_bid", sa.Integer(), nullable=True),
        sa.Column("yes_ask", sa.Integer(), nullable=True),
        sa.Column("midpoint", sa.Float(), nullable=True),
        sa.Column("spread", sa.Integer(), nullable=True),
        sa.Column("volume_24h", sa.Integer(), nullable=False),
        sa.Column("liquidity_proxy", sa.Integer(), nullable=False),
        sa.Column("raw_payload", RAW_JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_market_price_ticks_market_ticker", "market_price_ticks", ["market_ticker"])
    op.create_index(
        "ix_market_price_ticks_ticker_observed",
        "market_price_ticks",
        ["market_ticker", "observed_at"],
    )

    op.create_table(
        "opportunity_signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market_ticker", sa.String(64), nullable=False),
        sa.Column("signal_type", sa.String(48), nullable=False),
        sa.Column("signal_status", sa.String(32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("old_midpoint", sa.Float(), nullable=True),
        sa.Column("new_midpoint", sa.Float(), nullable=True),
        sa.Column("price_change", sa.Float(), nullable=True),
        sa.Column("spread", sa.Integer(), nullable=True),
        sa.Column("liquidity_proxy", sa.Integer(), nullable=True),
        sa.Column(
            "latest_forecast_id", sa.Integer(), sa.ForeignKey("market_forecasts.id"), nullable=True
        ),
        sa.Column("latest_forecast_probability", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", RAW_JSON, nullable=True),
        sa.Column("raw_payload", RAW_JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_opportunity_signals_market_ticker", "opportunity_signals", ["market_ticker"])
    op.create_index("ix_opportunity_signals_signal_type", "opportunity_signals", ["signal_type"])
    op.create_index(
        "ix_opportunity_signals_signal_status", "opportunity_signals", ["signal_status"]
    )

    op.create_table(
        "watcher_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("markets_checked", sa.Integer(), nullable=False),
        sa.Column("ticks_recorded", sa.Integer(), nullable=False),
        sa.Column("signals_created", sa.Integer(), nullable=False),
        sa.Column("error_type", sa.String(128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("watcher_runs")
    op.drop_table("opportunity_signals")
    op.drop_table("market_price_ticks")
