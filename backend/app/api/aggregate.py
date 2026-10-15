"""``GET /api/measurements/aggregate``: bucketed statistics (contract extension, Timescale)."""

from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.params import bad_request, parse_series, parse_time
from app.db import SessionDep
from app.schemas.aggregates import AggregateOut
from app.services import aggregates as service

router = APIRouter(prefix="/measurements", tags=["measurements"])


@router.get("/aggregate")
async def aggregate_measurements(
    session: SessionDep,
    response: Response,
    series: Annotated[str | None, Query(description="Comma-separated ids; empty = all")] = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    bucket: Annotated[service.Bucket, Query(description="Bucket size: 1h or 1d")] = "1h",
    limit: Annotated[int, Query(ge=1, le=10_000)] = 10_000,
) -> list[AggregateOut]:
    """Public. ``avg``, ``min``, ``max`` and ``count`` per series and UTC bucket, oldest bucket
    first. Buckets overlapping the closed interval [from, to] are returned; ``X-Total-Count``
    gives the total if the page was cut by ``limit``."""
    series_ids = parse_series(series)
    ts_from, ts_to = parse_time("from", from_), parse_time("to", to)
    if ts_from is not None and ts_to is not None and ts_from > ts_to:
        raise bad_request("from", "must not be later than 'to'")
    page = await service.aggregate(
        session, bucket=bucket, series_ids=series_ids, ts_from=ts_from, ts_to=ts_to, limit=limit
    )
    response.headers["X-Total-Count"] = str(page.total)
    return [AggregateOut.model_validate(row, from_attributes=True) for row in page.items]
