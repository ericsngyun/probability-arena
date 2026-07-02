"""market_detail_enrichments: detail/event/series metadata per market.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-02

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "market_detail_enrichments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market_ticker", sa.String(64), nullable=False),
        sa.Column("scanner_run_id", sa.Integer(), sa.ForeignKey("scanner_runs.id"), nullable=True),
        sa.Column("event_ticker", sa.String(64), nullable=True),
        sa.Column("series_ticker", sa.String(64), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("subtitle", sa.Text(), nullable=True),
        sa.Column("rules_text", sa.Text(), nullable=True),
        sa.Column("settlement_source", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("raw_market_detail", RAW_JSON, nullable=False),
        sa.Column("raw_event_detail", RAW_JSON, nullable=True),
        sa.Column("raw_series_detail", RAW_JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_market_detail_enrichments_market_ticker",
        "market_detail_enrichments",
        ["market_ticker"],
    )
    op.create_index(
        "ix_market_detail_enrichments_scanner_run_id",
        "market_detail_enrichments",
        ["scanner_run_id"],
    )


def downgrade() -> None:
    op.drop_table("market_detail_enrichments")
