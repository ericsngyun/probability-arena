"""POLY-001 read-only Polymarket market-data observer tables:
polymarket_scout_runs, polymarket_markets, polymarket_orderbook_snapshots,
polymarket_domain_inventory_snapshots. Market-DATA observation only — no EV,
arbitrage, trade recommendation, sizing, order, wallet, key, signing, swap, or
execution columns exist.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-07

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "polymarket_scout_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("markets_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("markets_persisted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("orderbooks_fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("orderbook_errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("domains_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider", sa.String(length=32)),
        sa.Column("provider_version", sa.String(length=16)),
        sa.Column("error_type", sa.String(length=128)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "polymarket_markets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("polymarket_scout_runs.id")),
        sa.Column("market_id", sa.String(length=128)),
        sa.Column("condition_id", sa.String(length=128)),
        sa.Column("question", sa.Text()),
        sa.Column("slug", sa.String(length=256)),
        sa.Column("category", sa.String(length=64)),
        sa.Column("description", sa.Text()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("closed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("restricted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enable_order_book", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("accepting_orders", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("outcomes", RAW_JSON),
        sa.Column("outcome_prices", RAW_JSON),
        sa.Column("clob_token_ids", RAW_JSON),
        sa.Column("num_outcomes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("best_bid", sa.Float()),
        sa.Column("best_ask", sa.Float()),
        sa.Column("last_trade_price", sa.Float()),
        sa.Column("spread", sa.Float()),
        sa.Column("two_sided", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("liquidity_usd", sa.Float()),
        sa.Column("volume_24h_usd", sa.Float()),
        sa.Column("volume_total_usd", sa.Float()),
        sa.Column("start_date", sa.DateTime(timezone=True)),
        sa.Column("end_date", sa.DateTime(timezone=True)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_polymarket_markets_run_id", "polymarket_markets", ["run_id"])
    op.create_index("ix_polymarket_markets_market_id", "polymarket_markets", ["market_id"])
    op.create_index("ix_polymarket_markets_category", "polymarket_markets", ["category"])
    op.create_index(
        "ix_polymarket_market_observed", "polymarket_markets", ["market_id", "observed_at"]
    )

    op.create_table(
        "polymarket_orderbook_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("polymarket_scout_runs.id")),
        sa.Column("market_id", sa.String(length=128)),
        sa.Column("token_id", sa.String(length=128)),
        sa.Column("outcome", sa.String(length=64)),
        sa.Column("best_bid", sa.Float()),
        sa.Column("best_ask", sa.Float()),
        sa.Column("mid", sa.Float()),
        sa.Column("spread", sa.Float()),
        sa.Column("bid_depth", sa.Float()),
        sa.Column("ask_depth", sa.Float()),
        sa.Column("total_depth", sa.Float()),
        sa.Column("num_bids", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("num_asks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("liquidity_proxy", sa.Float()),
        sa.Column("tick_size", sa.Float()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_polymarket_book_run_id", "polymarket_orderbook_snapshots", ["run_id"])
    op.create_index("ix_polymarket_book_market_id", "polymarket_orderbook_snapshots", ["market_id"])
    op.create_index("ix_polymarket_book_token_id", "polymarket_orderbook_snapshots", ["token_id"])
    op.create_index(
        "ix_polymarket_book_token_observed",
        "polymarket_orderbook_snapshots",
        ["token_id", "observed_at"],
    )

    op.create_table(
        "polymarket_domain_inventory_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("polymarket_scout_runs.id")),
        sa.Column("domain", sa.String(length=64)),
        sa.Column("market_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("two_sided_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("orderbook_enabled_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("two_sided_rate", sa.Float()),
        sa.Column("total_liquidity_usd", sa.Float()),
        sa.Column("total_volume_24h_usd", sa.Float()),
        sa.Column("avg_spread", sa.Float()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_polymarket_domain_inv_run_id", "polymarket_domain_inventory_snapshots", ["run_id"]
    )
    op.create_index(
        "ix_polymarket_domain_inv_domain", "polymarket_domain_inventory_snapshots", ["domain"]
    )


def downgrade() -> None:
    op.drop_table("polymarket_domain_inventory_snapshots")
    op.drop_table("polymarket_orderbook_snapshots")
    op.drop_table("polymarket_markets")
    op.drop_table("polymarket_scout_runs")
