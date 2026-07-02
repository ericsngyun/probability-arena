"""scanner_runs audit fields (duration_ms, source, error_type/error_message)
and market_snapshots.raw_payload for debugging.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-01

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

RAW_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("scanner_runs") as batch:
        batch.add_column(sa.Column("duration_ms", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("source", sa.String(16), nullable=False, server_default="api")
        )
        batch.add_column(sa.Column("error_type", sa.String(128), nullable=True))
        batch.alter_column("error", new_column_name="error_message")

    with op.batch_alter_table("market_snapshots") as batch:
        batch.add_column(sa.Column("raw_payload", RAW_JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("market_snapshots") as batch:
        batch.drop_column("raw_payload")

    with op.batch_alter_table("scanner_runs") as batch:
        batch.alter_column("error_message", new_column_name="error")
        batch.drop_column("error_type")
        batch.drop_column("source")
        batch.drop_column("duration_ms")
