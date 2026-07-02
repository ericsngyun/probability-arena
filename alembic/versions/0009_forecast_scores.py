"""forecast_scores: calibration scores of forecasts against outcomes.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-02

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "forecast_scores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "forecast_id", sa.Integer(), sa.ForeignKey("market_forecasts.id"), nullable=False
        ),
        sa.Column("market_ticker", sa.String(64), nullable=False),
        sa.Column("outcome_id", sa.Integer(), sa.ForeignKey("market_outcomes.id"), nullable=True),
        sa.Column("brier_score", sa.Float(), nullable=True),
        sa.Column("log_loss", sa.Float(), nullable=True),
        sa.Column("absolute_error", sa.Float(), nullable=True),
        sa.Column("was_resolved", sa.Boolean(), nullable=False),
        sa.Column("score_status", sa.String(16), nullable=False),
        sa.Column("score_notes", sa.Text(), nullable=True),
        sa.Column("score_tags", RAW_JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_forecast_scores_forecast_id", "forecast_scores", ["forecast_id"])
    op.create_index("ix_forecast_scores_market_ticker", "forecast_scores", ["market_ticker"])
    op.create_index("ix_forecast_scores_score_status", "forecast_scores", ["score_status"])


def downgrade() -> None:
    op.drop_table("forecast_scores")
