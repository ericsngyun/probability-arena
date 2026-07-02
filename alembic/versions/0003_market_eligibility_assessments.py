"""market_eligibility_assessments: audit table for the candidate hygiene gate.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-01

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "market_eligibility_assessments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market_ticker", sa.String(64), nullable=False),
        sa.Column("scanner_run_id", sa.Integer(), sa.ForeignKey("scanner_runs.id"), nullable=True),
        sa.Column("is_eligible", sa.Boolean(), nullable=False),
        sa.Column("rejection_reasons", RAW_JSON, nullable=True),
        sa.Column("warnings", RAW_JSON, nullable=True),
        sa.Column("has_two_sided_quote", sa.Boolean(), nullable=False),
        sa.Column("yes_bid", sa.Integer(), nullable=True),
        sa.Column("yes_ask", sa.Integer(), nullable=True),
        sa.Column("spread", sa.Integer(), nullable=True),
        sa.Column("liquidity", sa.Integer(), nullable=False),
        sa.Column("volume_24h", sa.Integer(), nullable=False),
        sa.Column("expiration_days", sa.Float(), nullable=True),
        sa.Column("market_type_flags", RAW_JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_market_eligibility_assessments_market_ticker",
        "market_eligibility_assessments",
        ["market_ticker"],
    )
    op.create_index(
        "ix_market_eligibility_assessments_scanner_run_id",
        "market_eligibility_assessments",
        ["scanner_run_id"],
    )


def downgrade() -> None:
    op.drop_table("market_eligibility_assessments")
