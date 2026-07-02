"""market_forecasts: structured probability forecasts per market.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-02

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "market_forecasts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market_ticker", sa.String(64), nullable=False),
        sa.Column("scanner_run_id", sa.Integer(), sa.ForeignKey("scanner_runs.id"), nullable=True),
        sa.Column(
            "research_packet_id",
            sa.Integer(),
            sa.ForeignKey("market_research_packets.id"),
            nullable=True,
        ),
        sa.Column(
            "resolution_assessment_id",
            sa.Integer(),
            sa.ForeignKey("market_resolution_assessments.id"),
            nullable=True,
        ),
        sa.Column("forecaster_name", sa.String(64), nullable=False),
        sa.Column("forecaster_version", sa.String(16), nullable=False),
        sa.Column("model_name", sa.String(64), nullable=True),
        sa.Column("prompt_version", sa.String(16), nullable=False),
        sa.Column("estimated_probability", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_depth", sa.String(16), nullable=False),
        sa.Column("forecast_risk", sa.String(16), nullable=False),
        sa.Column("forecast_summary", sa.Text(), nullable=False),
        sa.Column("bull_case", RAW_JSON, nullable=True),
        sa.Column("bear_case", RAW_JSON, nullable=True),
        sa.Column("skeptic_notes", RAW_JSON, nullable=True),
        sa.Column("key_assumptions", RAW_JSON, nullable=True),
        sa.Column("missing_info", RAW_JSON, nullable=True),
        sa.Column("what_would_change_mind", RAW_JSON, nullable=True),
        sa.Column("calibration_tags", RAW_JSON, nullable=True),
        sa.Column("raw_response", RAW_JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_market_forecasts_market_ticker", "market_forecasts", ["market_ticker"])
    op.create_index("ix_market_forecasts_scanner_run_id", "market_forecasts", ["scanner_run_id"])


def downgrade() -> None:
    op.drop_table("market_forecasts")
