"""Measurement ingestion and queries (plan §2 flow 1, §5.3, §7)."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Measurement, MeasurementRejection, Sensor, Series
from app.realtime.notify import MEASUREMENTS_CHANNEL, notify
from app.services.sensors import SensorIdentity

logger = logging.getLogger("zai.measurements")

FUTURE_TOLERANCE = timedelta(minutes=5)
EARLIEST = datetime(1970, 1, 1, tzinfo=UTC)  # keeps chunk creation sane; far past is not data
LATEST_UTC = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)

OUT_OF_RANGE = "out_of_range"
FUTURE_TIMESTAMP = "future_timestamp"
TOO_OLD = "timestamp_too_old"


class SensorGone(Exception):
    """The sensor was unregistered, or its series deleted, after the key was checked."""


class MeasurementRejected(Exception):
    """A semantic rejection (422). ``reason`` is one of the constants above."""

    def __init__(self, reason: str, detail: str, extensions: dict[str, float | str]) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.extensions = extensions


class MeasurementNotFound(Exception):
    pass


@dataclass(frozen=True)
class Page:
    items: Sequence[Measurement]
    total: int


def to_utc_clamped(moment: datetime) -> datetime:
    """UTC version of ``moment``; values beyond year 9999 after conversion are clamped."""
    try:
        return moment.astimezone(UTC)
    except OverflowError:
        return LATEST_UTC


async def _reject(
    session: AsyncSession,
    identity: SensorIdentity,
    value: float,
    moment: datetime,
    reason: str,
    detail: str,
    extensions: dict[str, float | str],
    *,
    record: bool,
) -> MeasurementRejected:
    """Log the rejection (error log); value/time rejections also keep a row for the admin UI."""
    logger.warning(
        "measurement.rejected",
        extra={
            "fields": {
                "reason": reason,
                "sensorId": identity.sensor_id,
                "seriesId": identity.series_id,
                "value": value,
                "timestamp": to_utc_clamped(moment).isoformat(),
                **extensions,
            }
        },
    )
    if record:
        session.add(
            MeasurementRejection(
                series_id=identity.series_id,
                sensor_id=identity.sensor_id,
                value=value,
                ts=to_utc_clamped(moment),
                reason=reason,
            )
        )
        await session.commit()  # the rejection is kept even though the request fails
    else:
        await session.rollback()
    return MeasurementRejected(reason, detail, extensions)


async def ingest(
    session: AsyncSession, identity: SensorIdentity, value: float, timestamp: datetime | None
) -> Measurement:
    """Validate and store one measurement, update the sensor, announce it - one transaction.

    Lock order is series (``FOR SHARE``, so a range change or delete waits for us and vice versa)
    then sensor (``FOR NO KEY UPDATE``, so an unregistration waits and later wins). A deleted
    series or revoked sensor means the key is no longer valid: ``SensorGone`` (401).
    """
    now = datetime.now(UTC)
    series = (
        await session.execute(
            select(Series.name, Series.min_value, Series.max_value)
            .where(Series.id == identity.series_id)
            .with_for_update(read=True)
        )
    ).first()
    if series is None:
        await session.rollback()
        raise SensorGone
    active = await session.execute(
        select(Sensor.id)
        .where(Sensor.id == identity.sensor_id, Sensor.revoked_at.is_(None))
        .with_for_update(key_share=True)  # FOR NO KEY UPDATE
    )
    if active.scalar_one_or_none() is None:
        await session.rollback()
        raise SensorGone

    moment = timestamp or now
    low, high = float(series.min_value), float(series.max_value)
    if moment < EARLIEST:
        raise await _reject(
            session,
            identity,
            value,
            moment,
            TOO_OLD,
            "The timestamp must not be earlier than 1970-01-01T00:00:00Z.",
            {},
            record=False,
        )
    if not low <= value <= high:
        raise await _reject(
            session,
            identity,
            value,
            moment,
            OUT_OF_RANGE,
            f"Value {value:.10g} is outside the allowed range [{low:.10g}, {high:.10g}] "
            f"of series '{series.name}'.",
            {"min": low, "max": high},
            record=True,
        )
    if moment > now + FUTURE_TOLERANCE:
        raise await _reject(
            session,
            identity,
            value,
            moment,
            FUTURE_TIMESTAMP,
            "The timestamp is more than 5 minutes ahead of the server clock.",
            {"serverTime": now.isoformat()},
            record=True,
        )

    measurement = Measurement(
        series_id=identity.series_id,
        sensor_id=identity.sensor_id,
        value=value,
        ts=moment.astimezone(UTC),  # safe: EARLIEST <= moment <= now + 5 min
    )
    session.add(measurement)
    await session.flush()
    await session.execute(
        update(Sensor).where(Sensor.id == identity.sensor_id).values(last_measurement_at=func.now())
    )
    await notify(
        session,
        MEASUREMENTS_CHANNEL,
        {
            "event": "measurement",
            "id": measurement.id,
            "seriesId": measurement.series_id,
            "sensorId": measurement.sensor_id,
            "value": measurement.value,
            "timestamp": measurement.ts.isoformat(),
        },
    )
    await session.commit()
    await session.refresh(measurement)
    return measurement


async def list_measurements(
    session: AsyncSession,
    *,
    series_ids: Sequence[int] | None,
    ts_from: datetime | None,
    ts_to: datetime | None,
    descending: bool,
    limit: int,
) -> Page:
    """Closed ``from <= ts <= to`` interval; no implicit window; ``total`` ignores ``limit``."""
    conditions: list[ColumnElement[bool]] = []
    if series_ids:
        conditions.append(Measurement.series_id.in_(series_ids))
    if ts_from is not None:
        conditions.append(Measurement.ts >= ts_from)
    if ts_to is not None:
        conditions.append(Measurement.ts <= ts_to)
    if descending:
        order = (Measurement.ts.desc(), Measurement.id.desc())
    else:
        order = (Measurement.ts.asc(), Measurement.id.asc())  # id: stable order for equal ts
    query = select(Measurement).where(*conditions).order_by(*order).limit(limit)
    items = (await session.execute(query)).scalars().all()
    total = (
        await session.execute(select(func.count()).select_from(Measurement).where(*conditions))
    ).scalar_one()
    return Page(items=items, total=int(total))


async def get_measurement(session: AsyncSession, measurement_id: int) -> Measurement:
    measurement = (
        await session.execute(select(Measurement).where(Measurement.id == measurement_id).limit(1))
    ).scalar_one_or_none()
    if measurement is None:
        raise MeasurementNotFound
    return measurement
