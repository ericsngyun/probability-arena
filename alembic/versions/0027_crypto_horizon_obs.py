"""CRYPTO-HORIZON-OBS-001 horizon-observation tables: cohorts, members,
observations.

Additive, auditable tables for a bounded read-only horizon-observation lane
that fills the UPSTREAM tick-coverage gap CRYPTO-COVERAGE-001 found. A fixed
research cohort gets manual market/liquidity observations near each
15m/1h/6h/24h mark, persisted as ordinary crypto_price_ticks plus an audit
observation row. Observation/market-data columns only — no side, size, EV,
dollar, profit, action, recommendation, order, wallet, key, swap, signing, or
execution column exists by construction.

Revision ID: 0027
Revises: 0026
Create Date: 2026-07-13

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def _raw_json():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "crypto_horizon_cohorts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(length=32), nullable=False, index=True),
        sa.Column("member_limit", sa.Integer(), nullable=False),
        sa.Column("window_hours", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("provenance", _raw_json()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "crypto_horizon_cohort_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "cohort_id", sa.Integer(),
            sa.ForeignKey("crypto_horizon_cohorts.id", name="fk_horizon_member_cohort"),
            index=True,
        ),
        sa.Column("chain", sa.String(length=32), nullable=False, index=True),
        sa.Column("token_address", sa.String(length=128), nullable=False, index=True),
        sa.Column("symbol", sa.String(length=64)),
        sa.Column(
            "birth_event_id", sa.Integer(),
            sa.ForeignKey("crypto_token_birth_events.id", name="fk_horizon_member_birth"),
        ),
        sa.Column("birth_observed_at", sa.DateTime(timezone=True)),
        sa.Column("first_evidence_at", sa.DateTime(timezone=True)),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_horizon_member_cohort_token",
        "crypto_horizon_cohort_members",
        ["cohort_id", "token_address"],
        unique=True,
    )
    op.create_table(
        "crypto_horizon_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "cohort_id", sa.Integer(),
            sa.ForeignKey("crypto_horizon_cohorts.id", name="fk_horizon_obs_cohort"),
            index=True,
        ),
        sa.Column(
            "member_id", sa.Integer(),
            sa.ForeignKey(
                "crypto_horizon_cohort_members.id", name="fk_horizon_obs_member"
            ),
            index=True,
        ),
        sa.Column("chain", sa.String(length=32), nullable=False, index=True),
        sa.Column("token_address", sa.String(length=128), nullable=False, index=True),
        sa.Column("horizon", sa.String(length=8), nullable=False, index=True),
        sa.Column("target_at", sa.DateTime(timezone=True)),
        sa.Column("window_start", sa.DateTime(timezone=True)),
        sa.Column("window_end", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=24), nullable=False, index=True),
        sa.Column("missing_cause", sa.String(length=32)),
        sa.Column(
            "tick_id", sa.Integer(),
            sa.ForeignKey("crypto_price_ticks.id", name="fk_horizon_obs_tick"),
        ),
        sa.Column("price_usd", sa.Float()),
        sa.Column("liquidity_usd", sa.Float()),
        sa.Column("volume_24h_usd", sa.Float()),
        sa.Column("market_cap", sa.Float()),
        sa.Column("fdv", sa.Float()),
        sa.Column("pair_address", sa.String(length=128)),
        sa.Column("dex_id", sa.String(length=64)),
        sa.Column("provider", sa.String(length=64)),
        sa.Column("raw_payload", _raw_json()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_horizon_obs_cohort_token_horizon",
        "crypto_horizon_observations",
        ["cohort_id", "token_address", "horizon"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_horizon_obs_cohort_token_horizon",
        table_name="crypto_horizon_observations",
    )
    op.drop_table("crypto_horizon_observations")
    op.drop_index(
        "ix_horizon_member_cohort_token",
        table_name="crypto_horizon_cohort_members",
    )
    op.drop_table("crypto_horizon_cohort_members")
    op.drop_table("crypto_horizon_cohorts")
