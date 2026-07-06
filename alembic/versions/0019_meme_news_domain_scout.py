"""MEME-NEWS-001 read-only scout tables: meme_scout_runs,
meme_attention_snapshots, meme_catalyst_events, domain_scout_runs,
domain_market_inventory_snapshots. Discovery / attention-scoring / catalyst /
domain-inventory intelligence only — no EV, trade, sizing, order, wallet,
swap, signing, or execution columns exist.

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-06

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "meme_scout_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("profiles_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("boosts_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_scored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("catalysts_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_type", sa.String(length=128)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "meme_attention_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("meme_scout_runs.id")),
        sa.Column("chain", sa.String(length=32)),
        sa.Column("token_address", sa.String(length=128)),
        sa.Column("pair_address", sa.String(length=128)),
        sa.Column("symbol", sa.String(length=64)),
        sa.Column("name", sa.String(length=256)),
        sa.Column("first_seen_at", sa.DateTime(timezone=True)),
        sa.Column("token_age_seconds", sa.Integer()),
        sa.Column("price_usd", sa.Float()),
        sa.Column("liquidity_usd", sa.Float()),
        sa.Column("volume_5m_usd", sa.Float()),
        sa.Column("volume_1h_usd", sa.Float()),
        sa.Column("volume_24h_usd", sa.Float()),
        sa.Column("price_change_5m", sa.Float()),
        sa.Column("price_change_1h", sa.Float()),
        sa.Column("liquidity_growth", sa.Float()),
        sa.Column("volume_growth", sa.Float()),
        sa.Column("boost_amount", sa.Float()),
        sa.Column("boost_velocity", sa.Float()),
        sa.Column("profile_completeness", sa.Float()),
        sa.Column("has_social", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("social_links_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("risk_level", sa.String(length=16)),
        sa.Column("risk_score", sa.Float()),
        sa.Column("provider_confidence", sa.Float()),
        sa.Column("attention_score", sa.Float()),
        sa.Column("score_components", RAW_JSON),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_meme_attention_run_id", "meme_attention_snapshots", ["run_id"])
    op.create_index("ix_meme_attention_chain", "meme_attention_snapshots", ["chain"])
    op.create_index("ix_meme_attention_token", "meme_attention_snapshots", ["token_address"])
    op.create_index(
        "ix_meme_attention_token_observed",
        "meme_attention_snapshots",
        ["token_address", "observed_at"],
    )

    op.create_table(
        "meme_catalyst_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("meme_scout_runs.id")),
        sa.Column("source", sa.String(length=32)),
        sa.Column("subject_type", sa.String(length=24)),
        sa.Column("subject_ref", sa.String(length=256)),
        sa.Column("catalyst_type", sa.String(length=48)),
        sa.Column("magnitude", sa.Float()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", RAW_JSON),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_meme_catalyst_run_id", "meme_catalyst_events", ["run_id"])
    op.create_index("ix_meme_catalyst_source", "meme_catalyst_events", ["source"])
    op.create_index("ix_meme_catalyst_subject_type", "meme_catalyst_events", ["subject_type"])
    op.create_index("ix_meme_catalyst_subject_ref", "meme_catalyst_events", ["subject_ref"])
    op.create_index("ix_meme_catalyst_type", "meme_catalyst_events", ["catalyst_type"])

    op.create_table(
        "domain_scout_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("markets_scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("domains_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("series_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_type", sa.String(length=128)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "domain_market_inventory_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("domain_scout_runs.id")),
        sa.Column("domain", sa.String(length=48)),
        sa.Column("series_prefix", sa.String(length=48)),
        sa.Column("market_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("two_sided_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("two_sided_rate", sa.Float()),
        sa.Column("volume_proxy_cents", sa.Integer()),
        sa.Column("liquidity_proxy_cents", sa.Integer()),
        sa.Column("resolution_clarity_proxy", sa.Float()),
        sa.Column("has_evidence_forecaster", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("data_source_notes", sa.String(length=256)),
        sa.Column("canary_priority", sa.Float()),
        sa.Column("priority_components", RAW_JSON),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_domain_inv_run_id", "domain_market_inventory_snapshots", ["run_id"])
    op.create_index("ix_domain_inv_domain", "domain_market_inventory_snapshots", ["domain"])
    op.create_index("ix_domain_inv_series", "domain_market_inventory_snapshots", ["series_prefix"])


def downgrade() -> None:
    op.drop_table("domain_market_inventory_snapshots")
    op.drop_table("domain_scout_runs")
    op.drop_table("meme_catalyst_events")
    op.drop_table("meme_attention_snapshots")
    op.drop_table("meme_scout_runs")
