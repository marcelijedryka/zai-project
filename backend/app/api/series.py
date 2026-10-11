"""Series: public reads, admin writes (contract §series)."""

from fastapi import APIRouter, Response

from app.core.problems import ProblemError
from app.db import SessionDep
from app.models import Series
from app.schemas.common import PathId
from app.schemas.series import SeriesInput, SeriesOut, SeriesStats
from app.security.deps import AdminDep
from app.services import series as service

router = APIRouter(prefix="/series", tags=["series"])


def _data(body: SeriesInput) -> service.SeriesData:
    return service.SeriesData(**body.model_dump())


def _not_found() -> ProblemError:
    return ProblemError(404, "The series does not exist.")


def _invalid_range() -> ProblemError:
    return ProblemError(
        422,
        "minValue must be smaller than maxValue.",
        type_="urn:zai:problem:invalid-range",
        errors=[{"field": "maxValue", "message": "must be greater than minValue"}],
    )


def _duplicate() -> ProblemError:
    return ProblemError(
        409, "A series with this name already exists.", type_="urn:zai:problem:duplicate-name"
    )


def _range_conflict(exc: service.RangeConflict) -> ProblemError:
    return ProblemError(
        409,
        f"The new range excludes stored measurements, which span {exc.data_min:g} to "
        f"{exc.data_max:g}. Widen the range or delete the series.",
        type_="urn:zai:problem:range-conflict",
        extensions={"dataMin": exc.data_min, "dataMax": exc.data_max},
    )


def _out(series: Series) -> SeriesOut:
    return SeriesOut.model_validate(series)


@router.get("")
async def list_series(session: SessionDep) -> list[SeriesOut]:
    return [_out(s) for s in await service.list_series(session)]


@router.post("", status_code=201)
async def create_series(
    body: SeriesInput, _admin: AdminDep, session: SessionDep, response: Response
) -> SeriesOut:
    try:
        series = await service.create_series(session, _data(body))
    except service.InvalidRange:
        raise _invalid_range() from None
    except service.DuplicateName:
        raise _duplicate() from None
    response.headers["Location"] = f"/api/series/{series.id}"
    return _out(series)


@router.get("/{series_id}")
async def get_series(series_id: PathId, session: SessionDep) -> SeriesOut:
    try:
        return _out(await service.get_series(session, series_id))
    except service.SeriesNotFound:
        raise _not_found() from None


@router.get("/{series_id}/stats")
async def get_series_stats(series_id: PathId, session: SessionDep) -> SeriesStats:
    """Extension: lets clients pre-validate range edits and anchor the default time window."""
    try:
        stats = await service.series_stats(session, series_id)
    except service.SeriesNotFound:
        raise _not_found() from None
    return SeriesStats(
        count=stats.count,
        min=stats.min,
        max=stats.max,
        first_timestamp=stats.first_timestamp,
        last_timestamp=stats.last_timestamp,
    )


@router.put("/{series_id}")
async def update_series(
    series_id: PathId, body: SeriesInput, _admin: AdminDep, session: SessionDep
) -> SeriesOut:
    # Order: 401 (AdminDep) -> 400 (body) -> 404 -> 422 (range) -> 409 (stored data, name).
    try:
        return _out(await service.update_series(session, series_id, _data(body)))
    except service.SeriesNotFound:
        raise _not_found() from None
    except service.InvalidRange:
        raise _invalid_range() from None
    except service.RangeConflict as exc:
        raise _range_conflict(exc) from None
    except service.DuplicateName:
        raise _duplicate() from None


@router.delete("/{series_id}", status_code=204, response_class=Response)
async def delete_series(series_id: PathId, _admin: AdminDep, session: SessionDep) -> Response:
    try:
        await service.delete_series(session, series_id)
    except service.SeriesNotFound:
        raise _not_found() from None
    return Response(status_code=204)
