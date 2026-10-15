"""Time-bucketed statistics from the ``measurements_hourly`` continuous aggregate (Timescale).

Hourly buckets are read straight from the aggregate. Daily buckets roll the hourly ones up, which
is exact because the aggregate stores sum and count (an average of averages would be wrong).
The aggregate is a real-time one: rows newer than its last refresh are added from the raw table,
so readers never wait for the refresh policy. Data backfilled into the past, or deleted, shows up
in the materialised part after the next policy run (every minute).
"""

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import Select, column, func, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Series

Bucket = Literal["1h", "1d"]
BUCKET_SIZE: dict[Bucket, timedelta] = {"1h": timedelta(hours=1), "1d": timedelta(days=1)}

HOURLY = table(
    "measurements_hourly",
    column("series_id"),
    column("bucket"),
    column("sample_count"),
    column("value_sum"),
    column("value_min"),
    column("value_max"),
)


@dataclass(frozen=True)
class AggregateRow:
    series_id: int
    bucket: datetime
    avg: float
    min: float
    max: float
    count: int


@dataclass(frozen=True)
class AggregatePage:
    items: list[AggregateRow]
    total: int


def bucket_start(moment: datetime, bucket: Bucket) -> datetime:
    """The start of the UTC bucket containing ``moment``."""
    moment = moment.astimezone(UTC)
    if bucket == "1d":
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return moment.replace(minute=0, second=0, microsecond=0)


def _statement(
    bucket: Bucket,
    series_ids: list[int] | None,
    ts_from: datetime | None,
    ts_to: datetime | None,
) -> Select[tuple[int, datetime, float, float, float, int]]:
    # Daily: group the hourly rows by day; hourly: every row is its own bucket.
    if bucket == "1d":
        bucket_col = func.time_bucket(BUCKET_SIZE["1d"], HOURLY.c.bucket).label("bucket")
        count = func.sum(HOURLY.c.sample_count)
        stmt = select(
            HOURLY.c.series_id,
            bucket_col,
            (func.sum(HOURLY.c.value_sum) / count).label("avg"),
            func.min(HOURLY.c.value_min).label("min"),
            func.max(HOURLY.c.value_max).label("max"),
            count.label("count"),
        ).group_by(HOURLY.c.series_id, bucket_col)
    else:
        stmt = select(
            HOURLY.c.series_id,
            HOURLY.c.bucket,
            (HOURLY.c.value_sum / HOURLY.c.sample_count).label("avg"),
            HOURLY.c.value_min.label("min"),
            HOURLY.c.value_max.label("max"),
            HOURLY.c.sample_count.label("count"),
        )
    # The join hides series that were deleted: their materialised rows linger until the next
    # refresh. Buckets overlapping [from, to]: from the bucket that contains ``from``.
    stmt = stmt.join(Series, Series.id == HOURLY.c.series_id)
    if series_ids is not None:
        stmt = stmt.where(HOURLY.c.series_id.in_(series_ids))
    if ts_from is not None:
        stmt = stmt.where(HOURLY.c.bucket >= bucket_start(ts_from, bucket))
    if ts_to is not None:
        # Up to the end of the bucket that contains ``to``, so a daily bucket is whole at both ends
        # (filtering the hourly rows at ``to`` itself would cut the last day short).
        # (A ``to`` at the very end of the calendar overflows: then no upper bound is needed.)
        with contextlib.suppress(OverflowError):
            stmt = stmt.where(HOURLY.c.bucket < bucket_start(ts_to, bucket) + BUCKET_SIZE[bucket])
    return stmt


async def aggregate(
    session: AsyncSession,
    *,
    bucket: Bucket,
    series_ids: list[int] | None,
    ts_from: datetime | None,
    ts_to: datetime | None,
    limit: int,
) -> AggregatePage:
    stmt = _statement(bucket, series_ids, ts_from, ts_to)
    ordered = stmt.order_by(stmt.selected_columns.bucket, HOURLY.c.series_id).limit(limit)
    rows = [
        AggregateRow(
            r["series_id"],
            r["bucket"],
            float(r["avg"]),
            float(r["min"]),
            float(r["max"]),
            int(r["count"]),
        )
        for r in (await session.execute(ordered)).mappings().all()
    ]
    total = len(rows)
    if len(rows) == limit:  # a full page: count exactly, so the caller can tell it was cut
        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
    return AggregatePage(rows, total)
