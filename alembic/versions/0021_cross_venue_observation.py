"""POLY-002 read-only Kalshi<->Polymarket cross-venue observation tables:
cross_venue_observation_runs, cross_venue_market_candidates. OBSERVATION /
semantic-matching / measurement only — no side, size, EV, dollar, profit,
action, arbitrage/arb label, order, wallet, signing, or execution column exists.

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-08

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "cross_venue_observation_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("kalshi_markets_considered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("polymarket_markets_considered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidates_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comparable_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unresolved_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_type", sa.String(length=128)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "cross_venue_market_candidates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("cross_venue_observation_runs.id")),
        sa.Column("kalshi_ticker", sa.String(length=64)),
        sa.Column("kalshi_event_ticker", sa.String(length=64)),
        sa.Column("polymarket_market_id", sa.String(length=128)),
        sa.Column("polymarket_token_id", sa.String(length=128)),
        sa.Column("polymarket_condition_id", sa.String(length=128)),
        sa.Column("domain", sa.String(length=64)),
        sa.Column("event_title_normalized", sa.Text()),
        sa.Column("outcome_normalized", sa.String(length=64)),
        sa.Column("resolution_time_kalshi", sa.DateTime(timezone=True)),
        sa.Column("resolution_time_polymarket", sa.DateTime(timezone=True)),
        sa.Column("match_confidence", sa.Float()),
        sa.Column("match_label", sa.String(length=32)),
        sa.Column("match_reasons", RAW_JSON),
        sa.Column("mismatch_reasons", RAW_JSON),
        sa.Column("kalshi_midpoint", sa.Float()),
        sa.Column("polymarket_midpoint", sa.Float()),
        sa.Column("midpoint_difference", sa.Float()),
        sa.Column("kalshi_spread", sa.Float()),
        sa.Column("polymarket_spread", sa.Float()),
        sa.Column("kalshi_liquidity_proxy", sa.Float()),
        sa.Column("polymarket_liquidity_proxy", sa.Float()),
        sa.Column("observed_difference", sa.Float()),
        sa.Column("observation_confidence", sa.Float()),
        sa.Column("raw_context", RAW_JSON),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_cross_venue_cand_run_id", "cross_venue_market_candidates", ["run_id"])
    op.create_index("ix_cross_venue_cand_kalshi", "cross_venue_market_candidates", ["kalshi_ticker"])
    op.create_index("ix_cross_venue_cand_polymarket", "cross_venue_market_candidates", ["polymarket_market_id"])
    op.create_index("ix_cross_venue_cand_domain", "cross_venue_market_candidates", ["domain"])
    op.create_index("ix_cross_venue_cand_label", "cross_venue_market_candidates", ["match_label"])
    op.create_index("ix_cross_venue_run_label", "cross_venue_market_candidates", ["run_id", "match_label"])


def downgrade() -> None:
    op.drop_table("cross_venue_market_candidates")
    op.drop_table("cross_venue_observation_runs")
