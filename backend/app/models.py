"""SQLAlchemy ORM models. Migrations in ``alembic/versions`` are written by hand and
kept in sync with these by ``tests/integration/test_migrations.py``."""

from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    MetaData,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# NaN compares equal to itself and sorts above every number in PostgreSQL, so plain
# comparisons let it through; list the non-finite values explicitly.
NON_FINITE = "('NaN'::float8, 'Infinity'::float8, '-Infinity'::float8)"
HEX64 = "^[0-9a-f]{64}$"

# Created by TimescaleDB itself (``<table>_<time column>_idx``), not described by our models.
TIMESCALE_MANAGED_INDEXES = {"measurements_ts_idx"}


def include_object(obj: object, name: str | None, type_: str, *_: object) -> bool:
    """Alembic ``include_object`` hook shared by ``alembic/env.py`` and the drift tests."""
    return not (type_ == "index" and name in TIMESCALE_MANAGED_INDEXES)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("char_length(username) >= 3", name="username_length"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    password_changed_at: Mapped[datetime] = _now()
    created_at: Mapped[datetime] = _now()


class UserSession(Base):
    __tablename__ = "sessions"
    __table_args__ = (CheckConstraint(f"token_hash ~ '{HEX64}'", name="token_hash_format"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(CHAR(64), unique=True)
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = _now()
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(255))


class Series(Base):
    __tablename__ = "series"
    __table_args__ = (
        CheckConstraint("char_length(name) >= 1", name="name_not_empty"),
        CheckConstraint("min_value < max_value", name="min_lt_max"),
        CheckConstraint(f"min_value NOT IN {NON_FINITE}", name="min_finite"),
        CheckConstraint(f"max_value NOT IN {NON_FINITE}", name="max_finite"),
        CheckConstraint("color ~ '^#[0-9A-Fa-f]{6}$'", name="color_format"),
        Index("uq_series_name_lower", func.lower(text("name")), unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    min_value: Mapped[float] = mapped_column(Float(53))
    max_value: Mapped[float] = mapped_column(Float(53))
    color: Mapped[str] = mapped_column(String(7))
    icon: Mapped[str | None] = mapped_column(String(50))
    unit: Mapped[str | None] = mapped_column(String(20))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    # Refreshed by the ORM ``onupdate`` hook only: Core/raw-SQL updates must set it explicitly.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Sensor(Base):
    __tablename__ = "sensors"
    __table_args__ = (
        CheckConstraint("char_length(name) >= 1", name="name_not_empty"),
        CheckConstraint(f"key_hash ~ '{HEX64}'", name="key_hash_format"),
        Index(
            "uq_sensors_key_hash_active",
            "key_hash",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    key_hash: Mapped[str] = mapped_column(CHAR(64))
    created_at: Mapped[datetime] = _now()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_measurement_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Measurement(Base):
    """TimescaleDB hypertable on ``ts`` (7-day chunks); the PK must include ``ts``.

    A hypertable cannot have a unique index on ``id`` alone, so ``id`` is unique only because
    it comes from one sequence and only the API inserts rows. Never insert explicit ids.
    """

    __tablename__ = "measurements"
    __table_args__ = (
        CheckConstraint(f"value NOT IN {NON_FINITE}", name="value_finite"),
        Index("ix_measurements_series_id_ts", "series_id", "ts"),
        Index("ix_measurements_id", "id"),
        # Without it every sensor delete (ON DELETE SET NULL) scans the whole hypertable.
        Index("ix_measurements_sensor_id", "sensor_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"))
    sensor_id: Mapped[int | None] = mapped_column(ForeignKey("sensors.id", ondelete="SET NULL"))
    value: Mapped[float] = mapped_column(Float(53))
    received_at: Mapped[datetime] = _now()


class MeasurementRejection(Base):
    __tablename__ = "measurement_rejections"
    __table_args__ = (
        CheckConstraint("reason IN ('out_of_range', 'future_timestamp')", name="reason_known"),
        Index("ix_measurement_rejections_created_at", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"), index=True)
    sensor_id: Mapped[int | None] = mapped_column(
        ForeignKey("sensors.id", ondelete="SET NULL"), index=True
    )
    value: Mapped[float] = mapped_column(Float(53))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = _now()
