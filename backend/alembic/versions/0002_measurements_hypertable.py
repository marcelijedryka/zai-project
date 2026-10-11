"""Measurements hypertable (7-day chunks).

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NON_FINITE = "('NaN'::float8, 'Infinity'::float8, '-Infinity'::float8)"


def upgrade() -> None:
    # Timescale requires every unique index to contain the time column, hence PK (id, ts).
    # ``id`` still comes from one BIGSERIAL sequence, so it is unique in practice.
    op.create_table(
        "measurements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("series_id", sa.BigInteger(), nullable=False),
        sa.Column("sensor_id", sa.BigInteger(), nullable=True),
        sa.Column("value", sa.Float(53), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", "ts", name="pk_measurements"),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["series.id"],
            name="fk_measurements_series_id_series",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sensor_id"],
            ["sensors.id"],
            name="fk_measurements_sensor_id_sensors",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(f"value NOT IN {NON_FINITE}", name="value_finite"),
    )
    op.execute("SELECT create_hypertable('measurements', by_range('ts', INTERVAL '7 days'))")
    op.create_index("ix_measurements_series_id_ts", "measurements", ["series_id", "ts"])
    op.create_index("ix_measurements_id", "measurements", ["id"])


def downgrade() -> None:
    op.drop_table("measurements")
