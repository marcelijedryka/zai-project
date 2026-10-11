"""Sensors: admin-only registration, listing and unregistration (F12)."""

from fastapi import APIRouter, Response

from app.core.audit import audit
from app.core.problems import ProblemError
from app.db import SessionDep
from app.schemas.common import PathId
from app.schemas.sensors import SensorCreated, SensorInput, SensorOut
from app.security.deps import AdminDep
from app.services import sensors as service

router = APIRouter(prefix="/sensors", tags=["sensors"])


def _not_found() -> ProblemError:
    return ProblemError(404, "The sensor does not exist.")


@router.get("")
async def list_sensors(_admin: AdminDep, session: SessionDep) -> list[SensorOut]:
    return [SensorOut.model_validate(s) for s in await service.list_sensors(session)]


@router.post("", status_code=201)
async def register_sensor(
    body: SensorInput, admin: AdminDep, session: SessionDep, response: Response
) -> SensorCreated:
    try:
        sensor, api_key = await service.register_sensor(session, body.name, body.series_id)
    except service.SeriesMissing:
        raise ProblemError(
            422,
            "The series does not exist.",
            type_="urn:zai:problem:series-not-found",
            errors=[{"field": "seriesId", "message": "no series with this id"}],
        ) from None
    # The key is deliberately absent from the audit line (and from every other log).
    audit(
        "sensor.registered",
        admin.user_id,
        sensorId=sensor.id,
        seriesId=sensor.series_id,
        name=sensor.name,
    )
    response.headers["Location"] = f"/api/sensors/{sensor.id}"
    return SensorCreated(
        id=sensor.id,
        name=sensor.name,
        series_id=sensor.series_id,
        created_at=sensor.created_at,
        last_measurement_at=sensor.last_measurement_at,
        api_key=api_key,
    )


@router.get("/{sensor_id}")
async def get_sensor(sensor_id: PathId, _admin: AdminDep, session: SessionDep) -> SensorOut:
    try:
        return SensorOut.model_validate(await service.get_sensor(session, sensor_id))
    except service.SensorNotFound:
        raise _not_found() from None


@router.delete("/{sensor_id}", status_code=204, response_class=Response)
async def unregister_sensor(sensor_id: PathId, admin: AdminDep, session: SessionDep) -> Response:
    try:
        series_id = await service.revoke_sensor(session, sensor_id)
    except service.SensorNotFound:
        raise _not_found() from None
    audit("sensor.unregistered", admin.user_id, sensorId=sensor_id, seriesId=series_id)
    return Response(status_code=204)
