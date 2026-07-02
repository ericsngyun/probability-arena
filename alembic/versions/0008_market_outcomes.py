"""market_outcomes: read-only settlement state per market.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-02

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "market_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market_ticker", sa.String(64), nullable=False),
        sa.Column("outcome_status", sa.String(16), nullable=False),
        sa.Column("resolved_probability", sa.Float(), nullable=True),
        sa.Column("winning_side", sa.String(8), nullable=True),
        sa.Column("settlement_price", sa.Float(), nullable=True),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("raw_payload", RAW_JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_market_outcomes_market_ticker", "market_outcomes", ["market_ticker"], unique=True
    )


def downgrade() -> None:
    op.drop_table("market_outcomes")
