"""Crypto Arena (CRYPTO-001) read-only surveillance tables: crypto_tokens,
crypto_pairs, crypto_token_discovery_events, crypto_token_risk_assessments,
crypto_price_ticks, crypto_opportunity_signals, crypto_watcher_runs.

No wallet, key, swap, transaction, order, or execution columns exist.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-04

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "crypto_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(32), nullable=False),
        sa.Column("token_address", sa.String(128), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=True),
        sa.Column("name", sa.String(256), nullable=True),
        sa.Column("decimals", sa.Integer(), nullable=True),
        sa.Column("metadata", RAW_JSON, nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_crypto_tokens_chain", "crypto_tokens", ["chain"])
    op.create_index("ix_crypto_tokens_token_address", "crypto_tokens", ["token_address"])
    op.create_index(
        "ix_crypto_tokens_chain_address", "crypto_tokens", ["chain", "token_address"], unique=True
    )

    op.create_table(
        "crypto_pairs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(32), nullable=False),
        sa.Column("pair_address", sa.String(128), nullable=False),
        sa.Column("base_token_address", sa.String(128), nullable=False),
        sa.Column("quote_token_address", sa.String(128), nullable=True),
        sa.Column("dex_id", sa.String(64), nullable=True),
        sa.Column("url", sa.String(512), nullable=True),
        sa.Column("pair_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", RAW_JSON, nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_crypto_pairs_chain", "crypto_pairs", ["chain"])
    op.create_index("ix_crypto_pairs_pair_address", "crypto_pairs", ["pair_address"])
    op.create_index(
        "ix_crypto_pairs_base_token_address", "crypto_pairs", ["base_token_address"]
    )
    op.create_index(
        "ix_crypto_pairs_chain_address", "crypto_pairs", ["chain", "pair_address"], unique=True
    )

    op.create_table(
        "crypto_token_discovery_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(32), nullable=False),
        sa.Column("token_address", sa.String(128), nullable=False),
        sa.Column("pair_address", sa.String(128), nullable=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload", RAW_JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_crypto_token_discovery_events_chain", "crypto_token_discovery_events", ["chain"]
    )
    op.create_index(
        "ix_crypto_token_discovery_events_token_address",
        "crypto_token_discovery_events",
        ["token_address"],
    )
    op.create_index(
        "ix_crypto_token_discovery_events_event_type",
        "crypto_token_discovery_events",
        ["event_type"],
    )

    op.create_table(
        "crypto_token_risk_assessments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(32), nullable=False),
        sa.Column("token_address", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("risk_score", sa.Float(), nullable=True),
        sa.Column("risk_level", sa.String(16), nullable=True),
        sa.Column("flags", RAW_JSON, nullable=True),
        sa.Column("raw_payload", RAW_JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_crypto_token_risk_assessments_chain", "crypto_token_risk_assessments", ["chain"]
    )
    op.create_index(
        "ix_crypto_token_risk_assessments_token_address",
        "crypto_token_risk_assessments",
        ["token_address"],
    )

    op.create_table(
        "crypto_price_ticks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(32), nullable=False),
        sa.Column("token_address", sa.String(128), nullable=False),
        sa.Column("pair_address", sa.String(128), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price_usd", sa.Float(), nullable=True),
        sa.Column("liquidity_usd", sa.Float(), nullable=True),
        sa.Column("volume_5m_usd", sa.Float(), nullable=True),
        sa.Column("volume_1h_usd", sa.Float(), nullable=True),
        sa.Column("volume_24h_usd", sa.Float(), nullable=True),
        sa.Column("price_change_5m", sa.Float(), nullable=True),
        sa.Column("price_change_1h", sa.Float(), nullable=True),
        sa.Column("market_cap", sa.Float(), nullable=True),
        sa.Column("fdv", sa.Float(), nullable=True),
        sa.Column("raw_payload", RAW_JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_crypto_price_ticks_chain", "crypto_price_ticks", ["chain"])
    op.create_index(
        "ix_crypto_price_ticks_token_address", "crypto_price_ticks", ["token_address"]
    )
    op.create_index("ix_crypto_price_ticks_pair_address", "crypto_price_ticks", ["pair_address"])
    op.create_index(
        "ix_crypto_price_ticks_pair_observed",
        "crypto_price_ticks",
        ["pair_address", "observed_at"],
    )

    op.create_table(
        "crypto_opportunity_signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(32), nullable=False),
        sa.Column("token_address", sa.String(128), nullable=False),
        sa.Column("pair_address", sa.String(128), nullable=True),
        sa.Column("signal_type", sa.String(48), nullable=False),
        sa.Column("signal_status", sa.String(32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", RAW_JSON, nullable=True),
        sa.Column("raw_payload", RAW_JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_crypto_opportunity_signals_chain", "crypto_opportunity_signals", ["chain"]
    )
    op.create_index(
        "ix_crypto_opportunity_signals_token_address",
        "crypto_opportunity_signals",
        ["token_address"],
    )
    op.create_index(
        "ix_crypto_opportunity_signals_signal_type",
        "crypto_opportunity_signals",
        ["signal_type"],
    )
    op.create_index(
        "ix_crypto_opportunity_signals_signal_status",
        "crypto_opportunity_signals",
        ["signal_status"],
    )

    op.create_table(
        "crypto_watcher_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("tokens_checked", sa.Integer(), nullable=False),
        sa.Column("pairs_checked", sa.Integer(), nullable=False),
        sa.Column("ticks_recorded", sa.Integer(), nullable=False),
        sa.Column("signals_created", sa.Integer(), nullable=False),
        sa.Column("error_type", sa.String(128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("crypto_watcher_runs")
    op.drop_table("crypto_opportunity_signals")
    op.drop_table("crypto_price_ticks")
    op.drop_table("crypto_token_risk_assessments")
    op.drop_table("crypto_token_discovery_events")
    op.drop_table("crypto_pairs")
    op.drop_table("crypto_tokens")
