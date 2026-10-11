"""Series business rules (plan §4.2, §5.3)."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Measurement, Series
from app.realtime.notify import SERIES_CHANNEL, notify


class SeriesNotFound(Exception):
    pass


class InvalidRange(Exception):
    """``minValue`` must be smaller than ``maxValue``."""


class DuplicateName(Exception):
    pass


class RangeConflict(Exception):
    """The new range would exclude measurements that are already stored."""

    def __init__(self, data_min: float, data_max: float) -> None:
        super().__init__("range excludes stored data")
        self.data_min = data_min
        self.data_max = data_max


@dataclass(frozen=True)
class SeriesData:
    name: str
    min_value: float
    max_value: float
    color: str
    icon: str | None = None
    unit: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class Stats:
    count: int
    min: float | None
    max: float | None
    first_timestamp: datetime | None
    last_timestamp: datetime | None


async def _announce(session: AsyncSession, series_id: int, action: str) -> None:
    payload = {"event": "series-changed", "id": series_id, "action": action}
    await notify(session, SERIES_CHANNEL, payload)


def _check_range(data: SeriesData) -> None:
    if not data.min_value < data.max_value:
        raise InvalidRange


async def _name_taken(session: AsyncSession, name: str, exclude_id: int | None = None) -> bool:
    clause = func.lower(Series.name) == func.lower(name)
    if exclude_id is not None:
        clause = clause & (Series.id != exclude_id)
    return bool((await session.execute(select(exists().where(clause)))).scalar())


def _apply(series: Series, data: SeriesData) -> None:
    series.name = data.name
    series.min_value = data.min_value
    series.max_value = data.max_value
    series.color = data.color
    series.icon = data.icon
    series.unit = data.unit
    series.description = data.description


@asynccontextmanager
async def _name_race_guard(session: AsyncSession) -> AsyncIterator[None]:
    """Map a lost race on the unique (lower(name)) index to ``DuplicateName``.

    The violation surfaces at the INSERT/UPDATE (flush) or at commit, so wrap both.
    """
    try:
        yield
    except IntegrityError as exc:
        await session.rollback()
        if "uq_series_name_lower" in str(exc.orig):
            raise DuplicateName from exc
        raise


async def list_series(session: AsyncSession) -> Sequence[Series]:
    return (await session.execute(select(Series).order_by(Series.id))).scalars().all()


async def get_series(session: AsyncSession, series_id: int) -> Series:
    series = await session.get(Series, series_id)
    if series is None:
        raise SeriesNotFound
    return series


async def create_series(session: AsyncSession, data: SeriesData) -> Series:
    _check_range(data)
    if await _name_taken(session, data.name):
        raise DuplicateName
    series = Series()
    _apply(series, data)
    async with _name_race_guard(session):
        session.add(series)
        await session.flush()
        await _announce(session, series.id, "created")
        await session.commit()
    await session.refresh(series)
    return series


async def update_series(session: AsyncSession, series_id: int, data: SeriesData) -> Series:
    """Full replace. The row stays locked (``FOR UPDATE``) while the stored measurements are
    inspected; measurement inserts take ``FOR SHARE`` on the same row, so none can slip in
    between the check and the update."""
    series = (
        await session.execute(select(Series).where(Series.id == series_id).with_for_update())
    ).scalar_one_or_none()
    if series is None:
        raise SeriesNotFound
    _check_range(data)
    data_min, data_max = (
        await session.execute(
            select(func.min(Measurement.value), func.max(Measurement.value)).where(
                Measurement.series_id == series_id
            )
        )
    ).one()
    if data_min is not None and (data_min < data.min_value or data_max > data.max_value):
        await session.rollback()
        raise RangeConflict(data_min, data_max)
    if await _name_taken(session, data.name, exclude_id=series_id):
        await session.rollback()
        raise DuplicateName
    async with _name_race_guard(session):
        _apply(series, data)
        series.updated_at = func.now()
        await session.flush()
        await _announce(session, series_id, "updated")
        await session.commit()
    await session.refresh(series)
    return series


async def delete_series(session: AsyncSession, series_id: int) -> None:
    """Hard delete; sensors, measurements and rejections go with it (``ON DELETE CASCADE``)."""
    result = await session.execute(delete(Series).where(Series.id == series_id))
    if result.rowcount == 0:  # type: ignore[attr-defined]
        await session.rollback()
        raise SeriesNotFound
    await _announce(session, series_id, "deleted")
    await session.commit()


async def series_stats(session: AsyncSession, series_id: int) -> Stats:
    await get_series(session, series_id)
    row = (
        await session.execute(
            select(
                func.count(),
                func.min(Measurement.value),
                func.max(Measurement.value),
                func.min(Measurement.ts),
                func.max(Measurement.ts),
            ).where(Measurement.series_id == series_id)
        )
    ).one()
    return Stats(*row)
