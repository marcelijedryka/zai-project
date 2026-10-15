"""Read access to the rejected measurements (the admin error-log view)."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MeasurementRejection, Sensor, Series


@dataclass(frozen=True)
class RejectionRow:
    id: int
    series_id: int
    series_name: str
    sensor_id: int | None
    sensor_name: str | None
    value: float
    timestamp: datetime
    reason: str
    created_at: datetime


async def list_rejections(session: AsyncSession, limit: int) -> list[RejectionRow]:
    """Newest first. Sensors are outer-joined: an unregistered sensor (soft-deleted) still has its
    row and name, so its rejections stay attributable."""
    result = await session.execute(
        select(MeasurementRejection, Series.name, Sensor.name)
        .join(Series, Series.id == MeasurementRejection.series_id)
        .outerjoin(Sensor, Sensor.id == MeasurementRejection.sensor_id)
        .order_by(MeasurementRejection.created_at.desc(), MeasurementRejection.id.desc())
        .limit(limit)
    )
    return [
        RejectionRow(
            id=r.id,
            series_id=r.series_id,
            series_name=series_name,
            sensor_id=r.sensor_id,
            sensor_name=sensor_name,
            value=r.value,
            timestamp=r.ts,
            reason=r.reason,
            created_at=r.created_at,
        )
        for r, series_name, sensor_name in result.all()
    ]
