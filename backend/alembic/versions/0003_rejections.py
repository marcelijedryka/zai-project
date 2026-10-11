"""Rejected measurements (F4 error log).

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "measurement_rejections",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("series_id", sa.BigInteger(), nullable=False),
        sa.Column("sensor_id", sa.BigInteger(), nullable=True),
        sa.Column("value", sa.Float(53), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_measurement_rejections"),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["series.id"],
            name="fk_measurement_rejections_series_id_series",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sensor_id"],
            ["sensors.id"],
            name="fk_measurement_rejections_sensor_id_sensors",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "reason IN ('out_of_range', 'future_timestamp')",
            name="reason_known",
        ),
    )
    op.create_index("ix_measurement_rejections_series_id", "measurement_rejections", ["series_id"])
    op.create_index(
        "ix_measurement_rejections_created_at",
        "measurement_rejections",
        [sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_table("measurement_rejections")
