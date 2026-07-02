"""pipeline_stage_runs: per-stage audit rows for pipeline executions.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-02

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "pipeline_stage_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "pipeline_run_id", sa.Integer(), sa.ForeignKey("pipeline_runs.id"), nullable=False
        ),
        sa.Column("stage_name", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("items_attempted", sa.Integer(), nullable=False),
        sa.Column("items_succeeded", sa.Integer(), nullable=False),
        sa.Column("items_failed", sa.Integer(), nullable=False),
        sa.Column("summary", RAW_JSON, nullable=True),
        sa.Column("error_type", sa.String(128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_pipeline_stage_runs_pipeline_run_id", "pipeline_stage_runs", ["pipeline_run_id"]
    )


def downgrade() -> None:
    op.drop_table("pipeline_stage_runs")
