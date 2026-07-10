"""TENNIS-TAPE-001 tape tables: runs, score snapshots, market snapshots, links.

Phase 0 MEASUREMENT infrastructure: replayable tapes aligning API-Tennis
score/state observations with Kalshi tennis market quote snapshots, captured
by manual bounded runs only. Observation columns only — no side, size, EV,
dollar, profit, action, recommendation, arbitrage/arb, order, wallet,
signing, or execution column exists by construction.

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-10

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def _raw_json():
    # column factory: Column/type objects must not be reused across upgrade cycles
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "tennis_tape_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("provider_source", sa.String(length=64)),
        sa.Column("score_calls_made", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("market_fetches_made", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidates_considered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("score_snapshots", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("market_snapshots", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("links_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_backed_links", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_type", sa.String(length=128)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "tennis_tape_score_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tape_run_id", sa.Integer(),
            sa.ForeignKey("tennis_tape_runs.id", name="fk_tape_score_run"),
            nullable=False, index=True,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("provider_source", sa.String(length=64), nullable=False),
        sa.Column("provider_event_id", sa.String(length=64), index=True),
        sa.Column("event_date", sa.String(length=10)),
        sa.Column("event_type", sa.String(length=64)),
        sa.Column("tournament_name", sa.String(length=128)),
        sa.Column("player_a", sa.String(length=128)),
        sa.Column("player_b", sa.String(length=128)),
        sa.Column("match_status", sa.String(length=64)),
        sa.Column("match_state", sa.String(length=16)),   # pre / in / post / unknown
        sa.Column("final_result", sa.String(length=32)),
        sa.Column("game_result", sa.String(length=32)),
        sa.Column("serving", sa.String(length=16)),
        sa.Column("set_scores", _raw_json()),
        sa.Column("missing_info", _raw_json()),
        sa.Column("raw_payload", _raw_json()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "tennis_tape_market_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tape_run_id", sa.Integer(),
            sa.ForeignKey("tennis_tape_runs.id", name="fk_tape_market_run"),
            nullable=False, index=True,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("market_ticker", sa.String(length=64), nullable=False, index=True),
        sa.Column("market_title", sa.Text()),
        sa.Column("market_status", sa.String(length=32)),
        sa.Column("yes_bid", sa.Integer()),
        sa.Column("yes_ask", sa.Integer()),
        sa.Column("midpoint", sa.Float()),
        sa.Column("spread", sa.Integer()),
        sa.Column("liquidity_proxy", sa.Integer()),
        sa.Column("volume_24h", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "tennis_tape_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tape_run_id", sa.Integer(),
            sa.ForeignKey("tennis_tape_runs.id", name="fk_tape_link_run"),
            nullable=False, index=True,
        ),
        sa.Column(
            "score_snapshot_id", sa.Integer(),
            sa.ForeignKey("tennis_tape_score_snapshots.id", name="fk_tape_link_score"),
        ),
        sa.Column(
            "market_snapshot_id", sa.Integer(),
            sa.ForeignKey("tennis_tape_market_snapshots.id", name="fk_tape_link_market"),
        ),
        sa.Column("market_ticker", sa.String(length=64), nullable=False, index=True),
        sa.Column("provider_event_id", sa.String(length=64)),
        sa.Column("link_label", sa.String(length=32), nullable=False, index=True),
        sa.Column("link_basis", sa.String(length=128)),
        sa.Column("player_a_code", sa.String(length=8)),
        sa.Column("player_b_code", sa.String(length=8)),
        sa.Column("event_date", sa.String(length=10)),
        sa.Column("score_observed_at", sa.DateTime(timezone=True)),
        sa.Column("market_observed_at", sa.DateTime(timezone=True)),
        sa.Column("score_to_market_delta_s", sa.Float()),
        sa.Column("missing_info", _raw_json()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("tennis_tape_links")
    op.drop_table("tennis_tape_market_snapshots")
    op.drop_table("tennis_tape_score_snapshots")
    op.drop_table("tennis_tape_runs")
