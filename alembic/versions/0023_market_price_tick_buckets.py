"""OPS-012 tick aggregation table: market_price_tick_buckets.

Fixed-interval AGGREGATES of raw market_price_ticks (OHLC midpoint, open/close
bid/ask, spread/liquidity ranges, tick counts) so raw ticks — the dominant
SQLite growth driver — need not be kept forever. Storage/telemetry summaries
only: no side, size, EV, dollar, profit, action, recommendation, arbitrage/arb,
order, wallet, signing, or execution column exists. The raw tick table is
unchanged and raw tick retention is unchanged in OPS-012.

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-09

"""
from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_price_tick_buckets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market_ticker", sa.String(length=64), nullable=False),
        sa.Column("domain", sa.String(length=32)),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bucket_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("open_mid", sa.Float()),
        sa.Column("high_mid", sa.Float()),
        sa.Column("low_mid", sa.Float()),
        sa.Column("close_mid", sa.Float()),
        sa.Column("open_bid", sa.Integer()),
        sa.Column("close_bid", sa.Integer()),
        sa.Column("open_ask", sa.Integer()),
        sa.Column("close_ask", sa.Integer()),
        sa.Column("spread_min", sa.Integer()),
        sa.Column("spread_max", sa.Integer()),
        sa.Column("spread_avg", sa.Float()),
        sa.Column("liquidity_min", sa.Integer()),
        sa.Column("liquidity_max", sa.Integer()),
        sa.Column("liquidity_avg", sa.Float()),
        sa.Column("tick_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "market_ticker", "bucket_start", "bucket_seconds",
            name="uq_tick_bucket_ticker_start_seconds",
        ),
    )
    op.create_index(
        "ix_market_price_tick_buckets_market_ticker",
        "market_price_tick_buckets", ["market_ticker"],
    )
    op.create_index(
        "ix_market_price_tick_buckets_domain",
        "market_price_tick_buckets", ["domain"],
    )
    op.create_index(
        "ix_tick_bucket_start", "market_price_tick_buckets", ["bucket_start"]
    )


def downgrade() -> None:
    op.drop_index("ix_tick_bucket_start", table_name="market_price_tick_buckets")
    op.drop_index(
        "ix_market_price_tick_buckets_domain", table_name="market_price_tick_buckets"
    )
    op.drop_index(
        "ix_market_price_tick_buckets_market_ticker",
        table_name="market_price_tick_buckets",
    )
    op.drop_table("market_price_tick_buckets")
