"""Initial schema: markets, market_snapshots, orderbook_snapshots, scanner_runs.

Mirrors the MVP-001 create_all schema exactly, so pre-Alembic databases can be
stamped at this revision and upgraded from here.

Revision ID: 0001
Revises:
Create Date: 2026-07-01

"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "markets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticker", sa.String(64), nullable=False),
        sa.Column("event_ticker", sa.String(64), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("category", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expiration_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rules_primary", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_markets_ticker", "markets", ["ticker"], unique=True)
    op.create_index("ix_markets_event_ticker", "markets", ["event_ticker"])
    op.create_index("ix_markets_status", "markets", ["status"])

    op.create_table(
        "scanner_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("markets_fetched", sa.Integer(), nullable=False),
        sa.Column("markets_ranked", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )

    op.create_table(
        "market_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("scanner_run_id", sa.Integer(), sa.ForeignKey("scanner_runs.id"), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("yes_bid", sa.Integer(), nullable=True),
        sa.Column("yes_ask", sa.Integer(), nullable=True),
        sa.Column("no_bid", sa.Integer(), nullable=True),
        sa.Column("no_ask", sa.Integer(), nullable=True),
        sa.Column("last_price", sa.Integer(), nullable=True),
        sa.Column("volume", sa.Integer(), nullable=False),
        sa.Column("volume_24h", sa.Integer(), nullable=False),
        sa.Column("open_interest", sa.Integer(), nullable=False),
        sa.Column("liquidity", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("score_components", sa.JSON(), nullable=True),
    )
    op.create_index("ix_market_snapshots_market_id", "market_snapshots", ["market_id"])
    op.create_index("ix_market_snapshots_scanner_run_id", "market_snapshots", ["scanner_run_id"])
    op.create_index(
        "ix_market_snapshots_market_captured", "market_snapshots", ["market_id", "captured_at"]
    )

    op.create_table(
        "orderbook_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("yes_levels", sa.JSON(), nullable=True),
        sa.Column("no_levels", sa.JSON(), nullable=True),
    )
    op.create_index("ix_orderbook_snapshots_market_id", "orderbook_snapshots", ["market_id"])
    op.create_index(
        "ix_orderbook_snapshots_market_captured",
        "orderbook_snapshots",
        ["market_id", "captured_at"],
    )


def downgrade() -> None:
    op.drop_table("orderbook_snapshots")
    op.drop_table("market_snapshots")
    op.drop_table("scanner_runs")
    op.drop_table("markets")
