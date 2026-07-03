"""Signal workflow fields on opportunity_signals: promotion timestamps,
refreshed packet/forecast links, and processing error capture.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-03

"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

FK_PACKET = "fk_opportunity_signals_refreshed_packet"
FK_FORECAST = "fk_opportunity_signals_refreshed_forecast"


def upgrade() -> None:
    # SQLite batch mode requires named constraints, hence add-column-then-FK
    with op.batch_alter_table("opportunity_signals") as batch:
        batch.add_column(sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("refreshed_research_packet_id", sa.Integer(), nullable=True)
        )
        batch.add_column(sa.Column("refreshed_forecast_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("processing_error_type", sa.String(128), nullable=True))
        batch.add_column(sa.Column("processing_error_message", sa.Text(), nullable=True))
        batch.create_foreign_key(
            FK_PACKET, "market_research_packets", ["refreshed_research_packet_id"], ["id"]
        )
        batch.create_foreign_key(
            FK_FORECAST, "market_forecasts", ["refreshed_forecast_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("opportunity_signals") as batch:
        batch.drop_constraint(FK_FORECAST, type_="foreignkey")
        batch.drop_constraint(FK_PACKET, type_="foreignkey")
        batch.drop_column("processing_error_message")
        batch.drop_column("processing_error_type")
        batch.drop_column("refreshed_forecast_id")
        batch.drop_column("refreshed_research_packet_id")
        batch.drop_column("processed_at")
        batch.drop_column("promoted_at")
