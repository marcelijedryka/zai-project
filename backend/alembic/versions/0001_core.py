"""Core tables: users, sessions, series, sensors (+ timescaledb extension).

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NON_FINITE = "('NaN'::float8, 'Infinity'::float8, '-Infinity'::float8)"


def _timestamp(name: str) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(50), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        _timestamp("password_changed_at"),
        _timestamp("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.CheckConstraint("char_length(username) >= 3", name="username_length"),
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.CHAR(64), nullable=False),
        _timestamp("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _timestamp("last_seen_at"),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_sessions_user_id_users", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash_format"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    op.create_table(
        "series",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("min_value", sa.Float(53), nullable=False),
        sa.Column("max_value", sa.Float(53), nullable=False),
        sa.Column("color", sa.String(7), nullable=False),
        sa.Column("icon", sa.String(50), nullable=True),
        sa.Column("unit", sa.String(20), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_series"),
        sa.CheckConstraint("char_length(name) >= 1", name="name_not_empty"),
        sa.CheckConstraint("min_value < max_value", name="min_lt_max"),
        sa.CheckConstraint(f"min_value NOT IN {NON_FINITE}", name="min_finite"),
        sa.CheckConstraint(f"max_value NOT IN {NON_FINITE}", name="max_finite"),
        sa.CheckConstraint("color ~ '^#[0-9A-Fa-f]{6}$'", name="color_format"),
    )
    op.create_index("uq_series_name_lower", "series", [sa.text("lower(name)")], unique=True)

    op.create_table(
        "sensors",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("series_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("key_hash", sa.CHAR(64), nullable=False),
        _timestamp("created_at"),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_measurement_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_sensors"),
        sa.ForeignKeyConstraint(
            ["series_id"], ["series.id"], name="fk_sensors_series_id_series", ondelete="CASCADE"
        ),
        sa.CheckConstraint("char_length(name) >= 1", name="name_not_empty"),
        sa.CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'", name="key_hash_format"),
    )
    op.create_index("ix_sensors_series_id", "sensors", ["series_id"])
    op.create_index(
        "uq_sensors_key_hash_active",
        "sensors",
        ["key_hash"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("sensors")
    op.drop_table("series")
    op.drop_table("sessions")
    op.drop_table("users")
    # The timescaledb extension is intentionally left installed.
