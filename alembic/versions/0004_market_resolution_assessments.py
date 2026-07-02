"""market_resolution_assessments: audit table for resolution-criteria assessments.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-02

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "market_resolution_assessments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market_ticker", sa.String(64), nullable=False),
        sa.Column("scanner_run_id", sa.Integer(), sa.ForeignKey("scanner_runs.id"), nullable=True),
        sa.Column("model_name", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(16), nullable=False),
        sa.Column("clarity_score", sa.Float(), nullable=False),
        sa.Column("resolution_risk", sa.String(16), nullable=False),
        sa.Column("tradeability", sa.String(32), nullable=False),
        sa.Column("settlement_source", sa.Text(), nullable=True),
        sa.Column("resolution_summary", sa.Text(), nullable=False),
        sa.Column("ambiguity_flags", RAW_JSON, nullable=True),
        sa.Column("rejection_reasons", RAW_JSON, nullable=True),
        sa.Column("llm_confidence", sa.Float(), nullable=True),
        sa.Column("raw_response", RAW_JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_market_resolution_assessments_market_ticker",
        "market_resolution_assessments",
        ["market_ticker"],
    )
    op.create_index(
        "ix_market_resolution_assessments_scanner_run_id",
        "market_resolution_assessments",
        ["scanner_run_id"],
    )


def downgrade() -> None:
    op.drop_table("market_resolution_assessments")
