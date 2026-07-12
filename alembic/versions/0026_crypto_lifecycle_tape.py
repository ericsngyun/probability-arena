"""CRYPTO-TAPE-001 lifecycle tape tables: runs, birth events, snapshots,
actor observations, survival outcomes.

Read-only Solana memecoin lifecycle tape DERIVED from rows the existing
lanes already persist (crypto ticks/pairs/discovery events/risk assessments
plus meme attention/catalysts). Research infrastructure only — observation
and survival-label columns exist; no EV, side, size, dollar, profit, action,
recommendation, order, key, swap, signing, or execution column exists by
construction. Actor columns hold public-chain addresses only.

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-11

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def _raw_json():
    # column factory: Column/type objects must not be reused across upgrade cycles
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "crypto_token_lifecycle_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("window_hours", sa.Integer()),
        sa.Column("tokens_considered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("birth_events_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("snapshots_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "actor_observations_created", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("outcomes_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider_coverage", _raw_json()),
        sa.Column("config", _raw_json()),
        sa.Column("error_type", sa.String(length=128)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "crypto_token_birth_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id", sa.Integer(),
            sa.ForeignKey("crypto_token_lifecycle_runs.id", name="fk_crypto_birth_run"),
            index=True,
        ),
        sa.Column("chain", sa.String(length=32), nullable=False, index=True),
        sa.Column("token_address", sa.String(length=128), nullable=False, index=True),
        sa.Column("symbol", sa.String(length=64)),
        sa.Column("name", sa.String(length=256)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_evidence_at", sa.DateTime(timezone=True), index=True),
        sa.Column("launch_source", sa.String(length=64)),
        sa.Column("first_pair_address", sa.String(length=128)),
        sa.Column("first_dex_id", sa.String(length=64)),
        sa.Column("pair_created_at", sa.DateTime(timezone=True)),
        sa.Column("creator_address", sa.String(length=128)),
        sa.Column("mint_authority_enabled", sa.Boolean()),
        sa.Column("freeze_authority_enabled", sa.Boolean()),
        sa.Column("metadata_links", _raw_json()),
        sa.Column("initial_price_usd", sa.Float()),
        sa.Column("initial_liquidity_usd", sa.Float()),
        sa.Column("initial_volume_24h_usd", sa.Float()),
        sa.Column("initial_market_cap", sa.Float()),
        sa.Column("initial_fdv", sa.Float()),
        sa.Column("bonding_curve_state", sa.String(length=32)),
        sa.Column("provenance", _raw_json()),
        sa.Column("missing_info", _raw_json()),
        sa.Column("raw_payload", _raw_json()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_crypto_birth_chain_token",
        "crypto_token_birth_events",
        ["chain", "token_address"],
        unique=True,
    )
    op.create_table(
        "crypto_token_lifecycle_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id", sa.Integer(),
            sa.ForeignKey("crypto_token_lifecycle_runs.id", name="fk_crypto_lifecycle_run"),
            index=True,
        ),
        sa.Column(
            "birth_event_id", sa.Integer(),
            sa.ForeignKey("crypto_token_birth_events.id", name="fk_crypto_lifecycle_birth"),
            index=True,
        ),
        sa.Column("chain", sa.String(length=32), nullable=False, index=True),
        sa.Column("token_address", sa.String(length=128), nullable=False, index=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token_age_seconds", sa.Integer()),
        sa.Column("price_usd", sa.Float()),
        sa.Column("liquidity_usd", sa.Float()),
        sa.Column("volume_5m_usd", sa.Float()),
        sa.Column("volume_1h_usd", sa.Float()),
        sa.Column("volume_24h_usd", sa.Float()),
        sa.Column("market_cap", sa.Float()),
        sa.Column("fdv", sa.Float()),
        sa.Column("holder_count", sa.Integer()),
        sa.Column("top10_holder_pct", sa.Float()),
        sa.Column("sniper_pct", sa.Float()),
        sa.Column("insider_pct", sa.Float()),
        sa.Column("bundler_pct", sa.Float()),
        sa.Column("creator_pct", sa.Float()),
        sa.Column("risk_score", sa.Float()),
        sa.Column("risk_level", sa.String(length=16)),
        sa.Column("risk_reasons", _raw_json()),
        sa.Column("boost_amount", sa.Float()),
        sa.Column("attention_score", sa.Float()),
        sa.Column("has_social", sa.Boolean()),
        sa.Column("social_links_count", sa.Integer()),
        sa.Column("catalyst_count_24h", sa.Integer()),
        sa.Column("pair_count", sa.Integer()),
        sa.Column("best_pair_address", sa.String(length=128)),
        sa.Column("best_dex_id", sa.String(length=64)),
        sa.Column("volume_to_liquidity_24h", sa.Float()),
        sa.Column("single_venue", sa.Boolean()),
        sa.Column(
            "source_tick_id", sa.Integer(),
            sa.ForeignKey("crypto_price_ticks.id", name="fk_crypto_lifecycle_tick"),
        ),
        sa.Column(
            "source_risk_assessment_id", sa.Integer(),
            sa.ForeignKey(
                "crypto_token_risk_assessments.id", name="fk_crypto_lifecycle_risk"
            ),
        ),
        sa.Column(
            "source_attention_snapshot_id", sa.Integer(),
            sa.ForeignKey(
                "meme_attention_snapshots.id", name="fk_crypto_lifecycle_attention"
            ),
        ),
        sa.Column("source_tick_age_seconds", sa.Integer()),
        sa.Column("provider_coverage", _raw_json()),
        sa.Column("missing_info", _raw_json()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_crypto_lifecycle_snap_token_observed",
        "crypto_token_lifecycle_snapshots",
        ["token_address", "observed_at"],
    )
    op.create_table(
        "crypto_token_actor_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id", sa.Integer(),
            sa.ForeignKey("crypto_token_lifecycle_runs.id", name="fk_crypto_actor_run"),
            index=True,
        ),
        sa.Column(
            "birth_event_id", sa.Integer(),
            sa.ForeignKey("crypto_token_birth_events.id", name="fk_crypto_actor_birth"),
            index=True,
        ),
        sa.Column("chain", sa.String(length=32), nullable=False, index=True),
        sa.Column("token_address", sa.String(length=128), nullable=False, index=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("creator_address", sa.String(length=128)),
        sa.Column("creator_holding_pct", sa.Float()),
        sa.Column("first_buyer_addresses", _raw_json()),
        sa.Column("sniper_address_count", sa.Integer()),
        sa.Column("insider_address_count", sa.Integer()),
        sa.Column("bundler_address_count", sa.Integer()),
        sa.Column("repeated_cohort_ref", sa.String(length=64)),
        sa.Column("known_creator_cluster_ref", sa.String(length=64)),
        sa.Column("holder_distribution", _raw_json()),
        sa.Column("observation_sources", _raw_json()),
        sa.Column("missing_info", _raw_json()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "crypto_token_survival_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "birth_event_id", sa.Integer(),
            sa.ForeignKey("crypto_token_birth_events.id", name="fk_crypto_survival_birth"),
            nullable=False, index=True, unique=True,
        ),
        sa.Column("chain", sa.String(length=32), nullable=False, index=True),
        sa.Column("token_address", sa.String(length=128), nullable=False, index=True),
        sa.Column("survived_15m", sa.Boolean()),
        sa.Column("survived_1h", sa.Boolean()),
        sa.Column("survived_6h", sa.Boolean()),
        sa.Column("survived_24h", sa.Boolean()),
        sa.Column("liquidity_removed", sa.Boolean()),
        sa.Column("dead_volume", sa.Boolean()),
        sa.Column("severe_risk", sa.Boolean()),
        sa.Column("graduated_or_migrated", sa.Boolean()),
        sa.Column("provider_gap", sa.Boolean()),
        sa.Column("final", sa.Boolean(), nullable=False, server_default="0", index=True),
        sa.Column("details", _raw_json()),
        sa.Column(
            "last_run_id", sa.Integer(),
            sa.ForeignKey("crypto_token_lifecycle_runs.id", name="fk_crypto_survival_run"),
        ),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("crypto_token_survival_outcomes")
    op.drop_table("crypto_token_actor_observations")
    op.drop_index(
        "ix_crypto_lifecycle_snap_token_observed",
        table_name="crypto_token_lifecycle_snapshots",
    )
    op.drop_table("crypto_token_lifecycle_snapshots")
    op.drop_index("ix_crypto_birth_chain_token", table_name="crypto_token_birth_events")
    op.drop_table("crypto_token_birth_events")
    op.drop_table("crypto_token_lifecycle_runs")
