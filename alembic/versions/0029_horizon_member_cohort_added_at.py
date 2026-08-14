"""CRYPTO-COVERAGE-REPAIR-002 B7: index crypto_horizon_cohort_members on
(cohort_id, added_at).

The prospective sparse-observation lane keeps ONE standing rolling cohort, so
`cohort_id` has no selectivity — every member row matches it. The per-pass
working-set query (`cohort_id = :id AND added_at >= :cutoff`) therefore
degenerated to a bare `SCAN crypto_horizon_cohort_members`, measured at 193,450
members with `sqlite_stat1` present. With this index the same query plans as a
range search: 58ms -> 0.3ms (194x).

Additive only: one CREATE INDEX, no column change, no data change, no
backfill. The existing unique index on (cohort_id, token_address) is untouched
and still serves the double-enrolment guarantee.

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-13

"""
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_horizon_member_cohort_added_at"
TABLE_NAME = "crypto_horizon_cohort_members"


def upgrade() -> None:
    op.create_index(INDEX_NAME, TABLE_NAME, ["cohort_id", "added_at"])


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
