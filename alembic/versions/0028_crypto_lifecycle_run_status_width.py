"""CRYPTO-COVERAGE-REPAIR-001 NEW-L2: widen crypto_token_lifecycle_runs.status
from String(16) to String(32). B3/B4/B5/B6 write-coordination statuses added
by this milestone include "skipped_contention" (18 chars) and
"dry_run_partial" (16 chars, fits but leaves no headroom), both longer than
the original running|ok|error vocabulary the column was sized for. SQLite is
silent about this (no length enforcement), but a stricter backend would
raise; this migration is data-safe (widen only, no truncation, no data
change) and additive-only.

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-10

"""
from alembic import op
import sqlalchemy as sa

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("crypto_token_lifecycle_runs") as batch:
        batch.alter_column(
            "status", existing_type=sa.String(16), type_=sa.String(32),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("crypto_token_lifecycle_runs") as batch:
        batch.alter_column(
            "status", existing_type=sa.String(32), type_=sa.String(16),
            existing_nullable=False,
        )
