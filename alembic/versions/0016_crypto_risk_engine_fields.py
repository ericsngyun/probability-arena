"""CRYPTO-002 risk-engine fields on crypto_token_risk_assessments:
normalized sub-scores, composite score/level, reasons, provider names, and
heuristic version. Read-only risk intelligence — no trade/execution columns.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-04

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

COLUMNS = (
    sa.Column("liquidity_risk_score", sa.Float(), nullable=True),
    sa.Column("holder_risk_score", sa.Float(), nullable=True),
    sa.Column("authority_risk_score", sa.Float(), nullable=True),
    sa.Column("market_structure_risk_score", sa.Float(), nullable=True),
    sa.Column("manipulation_risk_score", sa.Float(), nullable=True),
    sa.Column("provider_risk_score", sa.Float(), nullable=True),
    sa.Column("composite_risk_score", sa.Float(), nullable=True),
    sa.Column("composite_risk_level", sa.String(16), nullable=True),
    sa.Column("risk_reasons", RAW_JSON, nullable=True),
    sa.Column("provider_names", RAW_JSON, nullable=True),
    sa.Column("heuristic_version", sa.String(16), nullable=True),
)


def upgrade() -> None:
    with op.batch_alter_table("crypto_token_risk_assessments") as batch:
        for column in COLUMNS:
            batch.add_column(column)


def downgrade() -> None:
    with op.batch_alter_table("crypto_token_risk_assessments") as batch:
        for column in reversed(COLUMNS):
            batch.drop_column(column.name)
