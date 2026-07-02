"""market_research_packets: structured evidence packets per market.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-02

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "market_research_packets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("market_ticker", sa.String(64), nullable=False),
        sa.Column("scanner_run_id", sa.Integer(), sa.ForeignKey("scanner_runs.id"), nullable=True),
        sa.Column(
            "enrichment_id",
            sa.Integer(),
            sa.ForeignKey("market_detail_enrichments.id"),
            nullable=True,
        ),
        sa.Column(
            "resolution_assessment_id",
            sa.Integer(),
            sa.ForeignKey("market_resolution_assessments.id"),
            nullable=True,
        ),
        sa.Column("collector_name", sa.String(64), nullable=False),
        sa.Column("collector_version", sa.String(16), nullable=False),
        sa.Column("domain", sa.String(32), nullable=False),
        sa.Column("source_queries", RAW_JSON, nullable=True),
        sa.Column("sources", RAW_JSON, nullable=True),
        sa.Column("key_facts", RAW_JSON, nullable=True),
        sa.Column("missing_info", RAW_JSON, nullable=True),
        sa.Column("research_completeness_score", sa.Float(), nullable=False),
        sa.Column("research_risk", sa.String(16), nullable=False),
        sa.Column("raw_response", RAW_JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_market_research_packets_market_ticker", "market_research_packets", ["market_ticker"]
    )
    op.create_index(
        "ix_market_research_packets_scanner_run_id", "market_research_packets", ["scanner_run_id"]
    )
    op.create_index("ix_market_research_packets_domain", "market_research_packets", ["domain"])


def downgrade() -> None:
    op.drop_table("market_research_packets")
