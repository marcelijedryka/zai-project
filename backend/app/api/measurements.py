"""Measurements: sensor-authenticated ingestion, public reads (F1, contract §measurements)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request, Response

from app.api.params import bad_request, parse_series, parse_time
from app.core.problems import ProblemError
from app.db import SessionDep
from app.models import Measurement
from app.schemas.common import PathId
from app.schemas.measurements import MeasurementInput, MeasurementOut
from app.security.deps import SensorDep
from app.services import measurements as service

router = APIRouter(prefix="/measurements", tags=["measurements"])


def _policy(request: Request) -> service.IngestPolicy:
    return service.IngestPolicy(
        earliest=request.app.state.settings.earliest_measurement_timestamp,
        rejection_limiter=request.app.state.rejection_limiter,
    )


def _out(measurement: Measurement) -> MeasurementOut:
    return MeasurementOut(
        id=measurement.id,
        series_id=measurement.series_id,
        sensor_id=measurement.sensor_id,
        value=measurement.value,
        timestamp=measurement.ts,
    )


def _field_of(reason: str) -> str:
    return "timestamp" if "timestamp" in reason else "value"


@router.get("")
async def list_measurements(
    session: SessionDep,
    response: Response,
    series: Annotated[str | None, Query(description="Comma-separated ids; empty = all")] = None,
    from_: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    sort: Literal["timestamp", "-timestamp"] = "timestamp",
    limit: Annotated[int, Query(ge=1, le=10_000)] = 1000,
) -> list[MeasurementOut]:
    """Public. Closed ``from``/``to`` interval, no implicit time window, ``X-Total-Count``."""
    series_ids = parse_series(series)
    ts_from, ts_to = parse_time("from", from_), parse_time("to", to)
    if ts_from is not None and ts_to is not None and ts_from > ts_to:
        raise bad_request("from", "must not be later than 'to'")
    page = await service.list_measurements(
        session,
        series_ids=series_ids,
        ts_from=ts_from,
        ts_to=ts_to,
        descending=sort == "-timestamp",
        limit=limit,
    )
    response.headers["X-Total-Count"] = str(page.total)
    return [_out(m) for m in page.items]


@router.post("", status_code=201, response_model_exclude_none=True)
async def create_measurement(
    sensor: SensorDep,
    body: MeasurementInput,
    session: SessionDep,
    response: Response,
    request: Request,
) -> MeasurementOut:
    # Order: 415 (middleware) -> 401 (SensorDep) -> 400 (body) -> 422 (range, future timestamp).
    try:
        measurement = await service.ingest(
            session, sensor, body.value, body.timestamp, _policy(request)
        )
    except service.SensorGone:
        raise ProblemError(
            401,
            "A valid sensor API key is required.",
            headers={"WWW-Authenticate": 'ApiKey header="X-API-Key"'},
        ) from None
    except service.MeasurementRejected as exc:
        raise ProblemError(
            422,
            exc.detail,
            type_=f"urn:zai:problem:{exc.reason.replace('_', '-')}",
            errors=[{"field": _field_of(exc.reason), "message": exc.detail}],
            extensions=exc.extensions,
        ) from None
    response.headers["Location"] = f"/api/measurements/{measurement.id}"
    return _out(measurement)


@router.get("/{measurement_id}", response_model_exclude_none=True)
async def get_measurement(measurement_id: PathId, session: SessionDep) -> MeasurementOut:
    """Measurements are immutable: PUT, PATCH and DELETE here are 405 (no such route)."""
    try:
        return _out(await service.get_measurement(session, measurement_id))
    except service.MeasurementNotFound:
        raise ProblemError(404, "The measurement does not exist.") from None
