"""Sensor registration, listing, unregistration and API-key authentication (F12, T5.5)."""

import asyncio
import hashlib
import logging
import re
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.security.deps import SensorDep
from tests.conftest import Admin

FORBIDDEN_KEYS = re.compile(
    r'"(password|passwordHash|hash|salt|apiKeyHash|api_key_hash|keyHash|key_hash)"\s*:', re.I
)


async def make_series(client: httpx.AsyncClient, auth: dict[str, str], name: str = "S") -> int:
    response = await client.post(
        "/api/series",
        json={"name": name, "minValue": 0, "maxValue": 100, "color": "#1f77b4"},
        headers=auth,
    )
    assert response.status_code == 201
    return int(response.json()["id"])


async def register(
    client: httpx.AsyncClient, auth: dict[str, str], series_id: int, name: str = "Rooftop"
) -> dict[str, Any]:
    response = await client.post(
        "/api/sensors", json={"name": name, "seriesId": series_id}, headers=auth
    )
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


# --- register ----------------------------------------------------------------------


async def test_register_returns_201_location_and_the_key_exactly_once(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series_id = await make_series(client, auth)
    response = await client.post(
        "/api/sensors", json={"name": "  Rooftop  ", "seriesId": series_id}, headers=auth
    )
    assert response.status_code == 201
    data = response.json()
    assert response.headers["location"] == f"/api/sensors/{data['id']}"
    assert set(data) == {"id", "name", "seriesId", "createdAt", "lastMeasurementAt", "apiKey"}
    assert (data["name"], data["seriesId"]) == ("Rooftop", series_id)
    assert data["lastMeasurementAt"] is None
    assert re.fullmatch(r"zai_[A-Za-z0-9_-]{43}", data["apiKey"])
    assert not FORBIDDEN_KEYS.search(response.text)
    assert response.headers["content-type"].startswith("application/json")
    # The key is never returned again.
    for url in ("/api/sensors", f"/api/sensors/{data['id']}"):
        again = await client.get(url, headers=auth)
        assert again.status_code == 200
        assert data["apiKey"] not in again.text
        assert "apiKey" not in again.text
        assert not FORBIDDEN_KEYS.search(again.text)


async def test_every_sensor_gets_its_own_key(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series_id = await make_series(client, auth)
    first = await register(client, auth, series_id, "a")
    second = await register(client, auth, series_id, "b")
    assert first["apiKey"] != second["apiKey"]
    assert first["id"] != second["id"]


async def test_only_the_sha256_of_the_key_is_stored(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id = await make_series(client, auth)
    key = (await register(client, auth, series_id))["apiKey"]
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT * FROM sensors"))).mappings().all()
        everything: str = (
            await conn.execute(text("SELECT string_agg(t::text, ' ') FROM sensors t"))
        ).scalar_one()
    assert len(rows) == 1
    assert rows[0]["key_hash"] == hashlib.sha256(key.encode()).hexdigest()
    assert key not in everything
    assert rows[0]["revoked_at"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": "x"},
        {"seriesId": 1},
        {"name": "", "seriesId": 1},
        {"name": "   ", "seriesId": 1},
        {"name": "x" * 101, "seriesId": 1},
        {"name": 5, "seriesId": 1},
        {"name": "x", "seriesId": "1"},
        {"name": "x", "seriesId": 1.5},
        {"name": "x", "seriesId": True},
        {"name": "x", "seriesId": None},
        {"name": "x", "seriesId": [1]},
        [],
    ],
)
async def test_invalid_register_bodies_are_400(
    client: httpx.AsyncClient, auth: dict[str, str], payload: Any
) -> None:
    response = await client.post("/api/sensors", json=payload, headers=auth)
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_malformed_json_is_400(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    response = await client.post(
        "/api/sensors", content='{"name":', headers={**auth, "Content-Type": "application/json"}
    )
    assert response.status_code == 400


@pytest.mark.parametrize("series_id", [999_999_999, 0, -1, 2**63, 10**30])
async def test_a_series_that_does_not_exist_is_422(
    client: httpx.AsyncClient, auth: dict[str, str], series_id: int
) -> None:
    response = await client.post(
        "/api/sensors", json={"name": "x", "seriesId": series_id}, headers=auth
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    problem = response.json()
    assert problem["type"] == "urn:zai:problem:series-not-found"
    assert problem["errors"][0]["field"] == "seriesId"


async def test_admin_authentication_is_required_everywhere(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series_id = await make_series(client, auth)
    sensor = await register(client, auth, series_id)
    calls = [
        ("POST", "/api/sensors", {"name": "x", "seriesId": series_id}),
        ("GET", "/api/sensors", None),
        ("GET", f"/api/sensors/{sensor['id']}", None),
        ("GET", "/api/sensors/999999", None),
        ("DELETE", f"/api/sensors/{sensor['id']}", None),
        ("DELETE", "/api/sensors/999999", None),
        ("DELETE", "/api/sensors/abc", None),
    ]
    for method, url, payload in calls:
        response = await client.request(method, url, json=payload)
        assert response.status_code == 401, (method, url)
    # A sensor key is not an admin credential.
    as_key = await client.get("/api/sensors", headers={"X-API-Key": sensor["apiKey"]})
    assert as_key.status_code == 401


async def test_register_requires_a_json_body(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.post(
        "/api/sensors", content="name=x", headers={**auth, "Content-Type": "text/plain"}
    )
    assert response.status_code == 415


async def test_cookie_authenticated_registration_needs_an_allowed_origin(
    client: httpx.AsyncClient, token: str, auth: dict[str, str]
) -> None:
    series_id = await make_series(client, auth)
    cookie = {"Cookie": f"__Host-zai_session={token}"}
    payload = {"name": "x", "seriesId": series_id}
    assert (await client.post("/api/sensors", json=payload, headers=cookie)).status_code == 403
    allowed = {**cookie, "Origin": "http://localhost:5173"}
    assert (await client.post("/api/sensors", json=payload, headers=allowed)).status_code == 201


# --- list / get / unregister ---------------------------------------------------------


async def test_list_shows_active_sensors_ordered_by_id_without_keys(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    assert (await client.get("/api/sensors", headers=auth)).json() == []
    series_id = await make_series(client, auth)
    first = await register(client, auth, series_id, "first")
    second = await register(client, auth, series_id, "second")
    listed = (await client.get("/api/sensors", headers=auth)).json()
    assert [s["id"] for s in listed] == [first["id"], second["id"]]
    public_fields = {"id", "name", "seriesId", "createdAt", "lastMeasurementAt"}
    assert all(set(s) == public_fields for s in listed)


async def test_unregistering_hides_the_sensor_and_kills_its_key_at_once(
    app: FastAPI, client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    async def protected(sensor: SensorDep) -> dict[str, int]:
        return {"sensorId": sensor.sensor_id, "seriesId": sensor.series_id}

    app.add_api_route("/api/_t/sensor", protected)
    series_id = await make_series(client, auth)
    sensor = await register(client, auth, series_id)
    key = {"X-API-Key": sensor["apiKey"]}
    assert (await client.get("/api/_t/sensor", headers=key)).json() == {
        "sensorId": sensor["id"],
        "seriesId": series_id,
    }

    response = await client.delete(f"/api/sensors/{sensor['id']}", headers=auth)
    assert (response.status_code, response.content) == (204, b"")

    rejected = await client.get("/api/_t/sensor", headers=key)  # the very next request
    assert rejected.status_code == 401
    assert (await client.get(f"/api/sensors/{sensor['id']}", headers=auth)).status_code == 404
    assert (await client.get("/api/sensors", headers=auth)).json() == []
    assert (await client.delete(f"/api/sensors/{sensor['id']}", headers=auth)).status_code == 404
    # Soft delete: the row (and so the provenance of its measurements) stays.
    async with engine.connect() as conn:
        revoked: Any = (await conn.execute(text("SELECT revoked_at FROM sensors"))).scalar_one()
    assert revoked is not None


async def test_unknown_and_unparseable_sensor_ids_are_404(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    for sensor_id in ("999999", "0", "-1", "abc", "1.5", "9223372036854775808"):
        for method in ("GET", "DELETE"):
            response = await client.request(method, f"/api/sensors/{sensor_id}", headers=auth)
            assert response.status_code == 404, (method, sensor_id)
            assert response.headers["content-type"].startswith("application/problem+json")


async def test_deleting_a_series_removes_its_sensors_and_invalidates_their_keys(
    app: FastAPI, client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    async def protected(sensor: SensorDep) -> dict[str, int]:
        return {"sensorId": sensor.sensor_id}

    app.add_api_route("/api/_t/sensor", protected)
    series_id = await make_series(client, auth)
    sensor = await register(client, auth, series_id)
    assert (await client.delete(f"/api/series/{series_id}", headers=auth)).status_code == 204
    assert (await client.get(f"/api/sensors/{sensor['id']}", headers=auth)).status_code == 404
    key = {"X-API-Key": sensor["apiKey"]}
    assert (await client.get("/api/_t/sensor", headers=key)).status_code == 401


# --- sensor authentication dependency ---------------------------------------------------


async def test_sensor_dependency_accepts_only_a_valid_x_api_key(
    app: FastAPI, client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    async def protected(sensor: SensorDep) -> dict[str, int]:
        return {"sensorId": sensor.sensor_id}

    app.add_api_route("/api/_t/sensor", protected)
    sensor = await register(client, auth, await make_series(client, auth))
    good = sensor["apiKey"]
    last_changed = good[:-1] + ("A" if good[-1] != "A" else "B")
    bad_keys = [None, "", "x" * 40, last_changed, "x" * 300, good.upper()]
    for bad in bad_keys:
        headers = {"X-API-Key": bad} if bad is not None else {}
        response = await client.get("/api/_t/sensor", headers=headers)
        assert response.status_code == 401, bad
        assert response.headers["www-authenticate"] == 'ApiKey header="X-API-Key"'
        assert response.headers["content-type"].startswith("application/problem+json")
    # An admin Bearer token is not enough, with or without a (wrong) key.
    assert (await client.get("/api/_t/sensor", headers=auth)).status_code == 401
    assert (await client.get("/api/_t/sensor", headers={"X-API-Key": good})).status_code == 200


# --- audit and secrecy ---------------------------------------------------------------------


async def test_the_key_never_reaches_any_log(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    admin: Admin,
    caplog: pytest.LogCaptureFixture,
) -> None:
    series_id = await make_series(client, auth)
    with caplog.at_level(logging.DEBUG):
        sensor = await register(client, auth, series_id, "Audited")
        await client.get("/api/sensors", headers={"X-API-Key": sensor["apiKey"]})  # 401 path
        await client.delete(f"/api/sensors/{sensor['id']}", headers=auth)
        await client.delete("/api/sensors/999999", headers=auth)  # 404, not audited
    key = sensor["apiKey"]
    for record in caplog.records:
        assert key not in record.getMessage()
        assert key not in repr(record.__dict__)
    audit = [
        r for r in caplog.records if r.name == "zai.audit" and r.getMessage().startswith("sensor.")
    ]
    assert [r.getMessage() for r in audit] == ["sensor.registered", "sensor.unregistered"]
    for record in audit:
        fields = record.fields  # type: ignore[attr-defined]
        assert (fields["adminId"], fields["sensorId"], fields["seriesId"]) == (
            admin.id,
            sensor["id"],
            series_id,
        )


# --- concurrency ---------------------------------------------------------------------------


async def test_registering_while_the_series_is_being_deleted_is_422_not_500(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id = await make_series(client, auth)
    deleter = await engine.connect()
    transaction = await deleter.begin()
    await deleter.execute(text("DELETE FROM series WHERE id = :i"), {"i": series_id})
    registration = asyncio.create_task(
        client.post("/api/sensors", json={"name": "late", "seriesId": series_id}, headers=auth)
    )
    await asyncio.sleep(0.4)
    assert not registration.done()  # waiting for the row lock held by the delete
    await transaction.commit()
    await deleter.close()
    response = await asyncio.wait_for(registration, timeout=5)
    assert response.status_code == 422
    async with engine.connect() as conn:
        count: int = (await conn.execute(text("SELECT count(*) FROM sensors"))).scalar_one()
    assert count == 0
