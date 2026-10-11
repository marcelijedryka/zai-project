"""Sensor registration, unregistration and API-key authentication (plan §4.2, §6.1)."""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Sensor, Series
from app.schemas.common import MAX_ID
from app.security.api_keys import MAX_KEY_LENGTH, generate_api_key, hash_api_key


class SeriesMissing(Exception):
    """The series a sensor should write to does not exist."""


class SensorNotFound(Exception):
    pass


@dataclass(frozen=True)
class SensorIdentity:
    sensor_id: int
    series_id: int


async def register_sensor(session: AsyncSession, name: str, series_id: int) -> tuple[Sensor, str]:
    """Create a sensor for ``series_id`` and return it with its plaintext key (shown once)."""
    if not 1 <= series_id <= MAX_ID:
        raise SeriesMissing
    # FOR SHARE: the series cannot be deleted between this check and the insert.
    exists = await session.execute(
        select(Series.id).where(Series.id == series_id).with_for_update(read=True)
    )
    if exists.scalar_one_or_none() is None:
        await session.rollback()
        raise SeriesMissing
    key = generate_api_key()
    sensor = Sensor(series_id=series_id, name=name, key_hash=hash_api_key(key))
    session.add(sensor)
    try:
        await session.commit()
    except IntegrityError as exc:  # defensive: the series vanished despite the lock
        await session.rollback()
        raise SeriesMissing from exc
    await session.refresh(sensor)
    return sensor, key


async def list_sensors(session: AsyncSession) -> Sequence[Sensor]:
    query = select(Sensor).where(Sensor.revoked_at.is_(None)).order_by(Sensor.id)
    return (await session.execute(query)).scalars().all()


async def get_sensor(session: AsyncSession, sensor_id: int) -> Sensor:
    query = select(Sensor).where(Sensor.id == sensor_id, Sensor.revoked_at.is_(None))
    sensor = (await session.execute(query)).scalar_one_or_none()
    if sensor is None:
        raise SensorNotFound
    return sensor


async def revoke_sensor(session: AsyncSession, sensor_id: int) -> int:
    """Soft delete: the key stops working at once, the measurements stay. Returns the series id."""
    result = await session.execute(
        update(Sensor)
        .where(Sensor.id == sensor_id, Sensor.revoked_at.is_(None))
        .values(revoked_at=func.now())
        .returning(Sensor.series_id)
    )
    series_id = result.scalar_one_or_none()
    if series_id is None:
        await session.rollback()
        raise SensorNotFound
    await session.commit()
    return int(series_id)


async def authenticate_sensor(session: AsyncSession, api_key: str) -> SensorIdentity | None:
    """Resolve an ``X-API-Key`` to its active sensor; revoked or unknown keys give ``None``."""
    if not api_key or len(api_key) > MAX_KEY_LENGTH:
        return None
    row = (
        await session.execute(
            select(Sensor.id, Sensor.series_id).where(
                Sensor.key_hash == hash_api_key(api_key), Sensor.revoked_at.is_(None)
            )
        )
    ).first()
    return SensorIdentity(sensor_id=row[0], series_id=row[1]) if row else None
