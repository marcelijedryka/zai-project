"""Measurement ingestion (sensor key), rejections, locking, queries and immutability."""

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.security.rate_limit import LoginThrottle
from app.services import measurements as measurement_service

T0 = "2026-01-10T10:00:00Z"
T1 = "2026-01-10T11:00:00Z"
T2 = "2026-01-10T12:00:00Z"
FORBIDDEN_KEYS = re.compile(r'"(password|passwordHash|hash|salt|apiKeyHash|keyHash)"\s*:', re.I)


def iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()


async def setup(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    name: str = "PM2.5",
    low: float = 0,
    high: float = 100,
) -> tuple[int, dict[str, Any]]:
    created = await client.post(
        "/api/series",
        json={"name": name, "minValue": low, "maxValue": high, "color": "#1f77b4"},
        headers=auth,
    )
    assert created.status_code == 201
    series_id = int(created.json()["id"])
    sensor = await client.post(
        "/api/sensors", json={"name": f"sensor of {name}", "seriesId": series_id}, headers=auth
    )
    assert sensor.status_code == 201
    return series_id, sensor.json()


async def send(client: httpx.AsyncClient, key: str | None, **body: Any) -> httpx.Response:
    headers = {"X-API-Key": key} if key else {}
    return await client.post("/api/measurements", json=body, headers=headers)


async def count(engine: AsyncEngine, table: str) -> int:
    assert table in {"measurements", "measurement_rejections", "sensors"}
    async with engine.connect() as conn:
        return int((await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608


# --- accepted measurements -------------------------------------------------------------


async def test_a_sensor_posts_a_measurement(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series_id, sensor = await setup(client, auth)
    response = await send(client, sensor["apiKey"], value=21.5, timestamp=T1)
    assert response.status_code == 201
    data = response.json()
    assert response.headers["location"] == f"/api/measurements/{data['id']}"
    assert response.headers["content-type"].startswith("application/json")
    assert set(data) == {"id", "seriesId", "sensorId", "value", "timestamp"}
    assert (data["seriesId"], data["sensorId"], data["value"]) == (series_id, sensor["id"], 21.5)
    assert data["timestamp"] == T1
    assert not FORBIDDEN_KEYS.search(response.text)
    fetched = await client.get(response.headers["location"])
    assert fetched.status_code == 200
    assert fetched.json() == data


@pytest.mark.parametrize(
    ("sent", "expected_instant"),
    [
        ("2026-01-10T11:00:00Z", "2026-01-10T11:00:00Z"),
        ("2026-01-10T13:00:00+02:00", "2026-01-10T11:00:00Z"),
        ("2026-01-10T06:00:00-05:00", "2026-01-10T11:00:00Z"),
        ("2026-01-10T11:00:00.123456Z", "2026-01-10T11:00:00.123456Z"),
    ],
)
async def test_timestamps_with_any_offset_are_stored_as_the_same_utc_instant(
    client: httpx.AsyncClient, auth: dict[str, str], sent: str, expected_instant: str
) -> None:
    _, sensor = await setup(client, auth)
    response = await send(client, sensor["apiKey"], value=1, timestamp=sent)
    assert response.status_code == 201
    assert response.json()["timestamp"] == expected_instant


async def test_a_missing_or_null_timestamp_means_server_now(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, sensor = await setup(client, auth)
    for body in ({"value": 5}, {"value": 5, "timestamp": None}):
        response = await client.post(
            "/api/measurements", json=body, headers={"X-API-Key": sensor["apiKey"]}
        )
        assert response.status_code == 201
        stamp = datetime.fromisoformat(response.json()["timestamp"])
        assert abs((datetime.now(UTC) - stamp).total_seconds()) < 10


EXACT_VALUES = [0, 100, 0.0, 100.0, 50, 21.5, 0.1 + 0.2, 1e-9, 99.99999999999999]


@pytest.mark.parametrize("value", EXACT_VALUES)
async def test_values_inside_the_inclusive_range_round_trip_exactly(
    client: httpx.AsyncClient, auth: dict[str, str], value: float
) -> None:
    _, sensor = await setup(client, auth, low=0, high=100)
    response = await send(client, sensor["apiKey"], value=value, timestamp=T0)
    assert response.status_code == 201
    assert response.json()["value"] == value
    stored = (await client.get(f"/api/measurements/{response.json()['id']}")).json()["value"]
    assert stored == value


async def test_a_negative_range_and_both_bounds(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, sensor = await setup(client, auth, low=-5, high=5)
    for value in (-5, 5, 0, -4.999):
        assert (await send(client, sensor["apiKey"], value=value, timestamp=T0)).status_code == 201


async def test_the_sensor_row_records_the_last_measurement_time(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, sensor = await setup(client, auth)
    before = (await client.get(f"/api/sensors/{sensor['id']}", headers=auth)).json()
    assert before["lastMeasurementAt"] is None
    await send(client, sensor["apiKey"], value=1, timestamp=T0)  # a past timestamp
    after = (await client.get(f"/api/sensors/{sensor['id']}", headers=auth)).json()
    stamp = datetime.fromisoformat(after["lastMeasurementAt"])
    assert abs((datetime.now(UTC) - stamp).total_seconds()) < 10  # when it was received


# --- rejected measurements ----------------------------------------------------------------


@pytest.mark.parametrize("value", [-0.001, 100.001, 612, -1e6, 1e300])
async def test_a_value_outside_the_range_is_422_and_not_stored(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    engine: AsyncEngine,
    value: float,
    caplog: pytest.LogCaptureFixture,
) -> None:
    series_id, sensor = await setup(client, auth, name="Warsaw PM2.5", low=0, high=100)
    with caplog.at_level(logging.WARNING):
        response = await send(client, sensor["apiKey"], value=value, timestamp=T0)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    problem = response.json()
    assert problem["type"] == "urn:zai:problem:out-of-range"
    assert "Warsaw PM2.5" in problem["detail"]
    assert "[0, 100]" in problem["detail"]
    assert (problem["min"], problem["max"]) == (0, 100)
    assert problem["errors"][0]["field"] == "value"
    assert await count(engine, "measurements") == 0  # not stored
    assert (await client.get(f"/api/measurements?series={series_id}")).json() == []
    # ... but remembered for the admin UI, and in the error log:
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM measurement_rejections"))).mappings().one()
    assert (row["series_id"], row["sensor_id"], row["reason"], row["value"]) == (
        series_id,
        sensor["id"],
        "out_of_range",
        value,
    )
    record = next(r for r in caplog.records if r.getMessage() == "measurement.rejected")
    fields = record.fields  # type: ignore[attr-defined]
    assert record.levelno == logging.WARNING
    assert (fields["reason"], fields["seriesId"], fields["sensorId"], fields["value"]) == (
        "out_of_range",
        series_id,
        sensor["id"],
        value,
    )
    assert sensor["apiKey"] not in repr(record.__dict__)
    # A rejection is not a "last measurement":
    sensor_now = (await client.get(f"/api/sensors/{sensor['id']}", headers=auth)).json()
    assert sensor_now["lastMeasurementAt"] is None


async def test_a_timestamp_more_than_five_minutes_ahead_is_422(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    _, sensor = await setup(client, auth)
    for ahead in (timedelta(hours=1), timedelta(minutes=6), timedelta(days=3650)):
        response = await send(client, sensor["apiKey"], value=1, timestamp=iso(ahead))
        assert response.status_code == 422, ahead
        assert response.json()["type"] == "urn:zai:problem:future-timestamp"
        assert response.json()["errors"][0]["field"] == "timestamp"
    assert await count(engine, "measurements") == 0
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT reason FROM measurement_rejections"))
        reasons: list[str] = list(rows.scalars().all())
    assert reasons == ["future_timestamp"] * 3


async def test_small_clock_skew_into_the_future_is_tolerated(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, sensor = await setup(client, auth)
    response = await send(client, sensor["apiKey"], value=1, timestamp=iso(timedelta(minutes=4)))
    assert response.status_code == 201


async def test_range_is_checked_before_the_timestamp_and_recorded_once(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    _, sensor = await setup(client, auth)
    response = await send(client, sensor["apiKey"], value=500, timestamp=iso(timedelta(hours=2)))
    assert response.json()["type"] == "urn:zai:problem:out-of-range"
    assert await count(engine, "measurement_rejections") == 1


@pytest.mark.parametrize(
    "stamp",
    [
        "2019-12-31T23:59:59Z",
        "1970-01-01T00:00:00Z",
        "0001-01-01T00:00:00+14:00",
        "0001-01-01T00:00:00Z",
    ],
)
async def test_timestamps_before_the_earliest_allowed_are_422_without_crashing(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    engine: AsyncEngine,
    stamp: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, sensor = await setup(client, auth)
    with caplog.at_level(logging.WARNING):
        response = await send(client, sensor["apiKey"], value=1, timestamp=stamp)
    assert response.status_code == 422
    problem = response.json()
    assert problem["type"] == "urn:zai:problem:timestamp-too-old"
    assert "2020-01-01T00:00:00Z" in problem["detail"]
    assert await count(engine, "measurements") == 0
    assert await count(engine, "measurement_rejections") == 0  # not a value problem: not recorded
    record = next(r for r in caplog.records if r.getMessage() == "measurement.rejected")
    assert record.fields["reason"] == "timestamp_too_old"  # type: ignore[attr-defined]
    edge = await send(client, sensor["apiKey"], value=1, timestamp="2020-01-01T00:00:00Z")
    assert edge.status_code == 201  # the floor itself is allowed


async def test_the_floor_bounds_how_many_chunks_a_sensor_can_create(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    """Scattered ancient timestamps used to create ~2,900 chunks and break queries over them."""
    _, sensor = await setup(client, auth)
    for stamp in ("1970-01-01T00:00:00Z", "1985-06-15T12:00:00Z", "2005-03-03T03:03:03Z"):
        assert (await send(client, sensor["apiKey"], value=1, timestamp=stamp)).status_code == 422
    async with engine.connect() as conn:
        chunks: int = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM timescaledb_information.chunks "
                    "WHERE hypertable_name = 'measurements'"
                )
            )
        ).scalar_one()
    assert chunks == 0


async def test_the_earliest_timestamp_is_configurable(
    app: FastAPI, client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, sensor = await setup(client, auth)
    stamp = "2024-06-01T00:00:00Z"
    assert (await send(client, sensor["apiKey"], value=1, timestamp=stamp)).status_code == 201
    app.state.settings = app.state.settings.model_copy(
        update={"earliest_measurement_timestamp": datetime(2025, 1, 1, tzinfo=UTC)}
    )
    response = await send(client, sensor["apiKey"], value=1, timestamp=stamp)
    assert response.status_code == 422
    assert "2025-01-01T00:00:00Z" in response.json()["detail"]


async def test_timestamps_near_year_9999_do_not_overflow(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    _, sensor = await setup(client, auth)
    for stamp in ("9999-12-31T23:59:59-12:00", "9999-12-31T23:59:59Z", "9999-12-31T23:59:59+14:00"):
        response = await send(client, sensor["apiKey"], value=1, timestamp=stamp)
        assert response.status_code == 422, stamp
        assert response.json()["type"] == "urn:zai:problem:future-timestamp"
    assert await count(engine, "measurement_rejections") == 3


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"timestamp": T0},
        {"value": "abc"},
        {"value": "21.5"},
        {"value": True},
        {"value": None},
        {"value": [1]},
        {"value": {"v": 1}},
        {"value": 1, "timestamp": "wczoraj"},
        {"value": 1, "timestamp": "2026-01-10T10:00:00"},
        {"value": 1, "timestamp": "2026-01-10"},
        {"value": 1, "timestamp": 1768039200},
        {"value": 1, "timestamp": 1768039200000},
        {"value": 1, "timestamp": True},
        {"value": 1, "timestamp": ["2026-01-10T10:00:00Z"]},
    ],
)
async def test_invalid_bodies_are_400(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine, payload: dict[str, Any]
) -> None:
    _, sensor = await setup(client, auth)
    response = await client.post(
        "/api/measurements", json=payload, headers={"X-API-Key": sensor["apiKey"]}
    )
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["errors"]
    assert await count(engine, "measurements") == 0
    assert await count(engine, "measurement_rejections") == 0  # invalid input is not a rejection


@pytest.mark.parametrize(
    "raw",
    [
        '{"value":',
        '{"value": NaN}',
        '{"value": Infinity}',
        '{"value": -Infinity}',
        '{"value": 1e999}',
        "[1]",
    ],
)
async def test_malformed_json_and_non_finite_numbers_are_400(
    client: httpx.AsyncClient, auth: dict[str, str], raw: str
) -> None:
    _, sensor = await setup(client, auth)
    response = await client.post(
        "/api/measurements",
        content=raw,
        headers={"X-API-Key": sensor["apiKey"], "Content-Type": "application/json"},
    )
    assert response.status_code == 400


# --- authentication and precedence ------------------------------------------------------------


async def test_only_a_valid_sensor_key_may_post(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    _, sensor = await setup(client, auth)
    attempts = [
        ({}, "no key"),
        ({"X-API-Key": "x" * 40}, "wrong key"),
        ({"X-API-Key": sensor["apiKey"][:-1]}, "truncated key"),
        (auth, "admin Bearer token only"),
        ({**auth, "X-API-Key": "x" * 40}, "Bearer plus wrong key"),
    ]
    for headers, label in attempts:
        response = await client.post("/api/measurements", json={"value": 1}, headers=headers)
        assert response.status_code == 401, label
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.headers["www-authenticate"] == 'ApiKey header="X-API-Key"'
    assert await count(engine, "measurements") == 0


async def test_status_precedence_415_then_401_then_400_then_422(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, sensor = await setup(client, auth)
    key = {"X-API-Key": sensor["apiKey"]}
    plain = {"Content-Type": "text/plain"}
    # 415 comes first, even with a valid key and even without any key.
    with_key = await client.post("/api/measurements", content="value=1", headers={**key, **plain})
    without_key = await client.post("/api/measurements", content="value=1", headers=plain)
    assert (with_key.status_code, without_key.status_code) == (415, 415)
    # 401 before body validation: an invalid body without a key is 401, not 400.
    assert (await client.post("/api/measurements", json={"value": "abc"})).status_code == 401
    assert (await client.post("/api/measurements", json={})).status_code == 401
    # 400 before 422: an invalid body is never "rejected" as an out-of-range value.
    invalid = await client.post("/api/measurements", json={"value": "abc"}, headers=key)
    out_of_range = await client.post("/api/measurements", json={"value": 999}, headers=key)
    assert (invalid.status_code, out_of_range.status_code) == (400, 422)


async def test_an_unregistered_sensor_is_refused_at_once_but_its_data_stays(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series_id, sensor = await setup(client, auth)
    key = sensor["apiKey"]
    posted = await send(client, key, value=7, timestamp=T0)
    assert posted.status_code == 201
    assert (await client.delete(f"/api/sensors/{sensor['id']}", headers=auth)).status_code == 204
    assert (await send(client, key, value=7, timestamp=T1)).status_code == 401
    assert (await client.get(posted.headers["location"])).status_code == 200
    listed = (await client.get(f"/api/measurements?series={series_id}")).json()
    assert [m["sensorId"] for m in listed] == [sensor["id"]]  # provenance kept


async def test_deleting_the_series_deletes_data_and_kills_the_key(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series_id, sensor = await setup(client, auth)
    posted = await send(client, sensor["apiKey"], value=7, timestamp=T0)
    assert (await client.delete(f"/api/series/{series_id}", headers=auth)).status_code == 204
    assert (await client.get(posted.headers["location"])).status_code == 404
    assert (await client.get(f"/api/measurements?series={series_id}")).json() == []
    assert (await send(client, sensor["apiKey"], value=7)).status_code == 401


# --- notifications -------------------------------------------------------------------------


async def test_accepted_measurements_are_announced_on_commit_and_rejected_ones_are_not(
    client: httpx.AsyncClient, auth: dict[str, str], settings: Settings
) -> None:
    received: list[dict[str, Any]] = []
    listener = await asyncpg.connect(
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
    )
    await listener.add_listener(
        "zai_measurements", lambda _c, _p, _ch, payload: received.append(json.loads(payload))
    )
    try:
        series_id, sensor = await setup(client, auth)
        ok = await send(client, sensor["apiKey"], value=42.5, timestamp=T0)
        await send(client, sensor["apiKey"], value=500, timestamp=T0)  # 422
        await send(client, sensor["apiKey"], value="x")  # 400
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
    finally:
        await listener.close()
    assert received == [
        {
            "event": "measurement",
            "id": ok.json()["id"],
            "seriesId": series_id,
            "sensorId": sensor["id"],
            "value": 42.5,
            "timestamp": "2026-01-10T10:00:00+00:00",
        }
    ]


# --- locking and concurrency -----------------------------------------------------------------


async def hold(engine: AsyncEngine, sql: str, **params: Any) -> tuple[Any, Any]:
    """Open a transaction that executes ``sql`` and stays open; returns (connection, txn)."""
    conn = await engine.connect()
    txn = await conn.begin()
    await conn.execute(text(sql), params)
    return conn, txn


async def release(conn: Any, txn: Any) -> None:
    await txn.commit()
    await conn.close()


async def test_an_insert_waits_for_a_range_change_and_obeys_the_new_range(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id, sensor = await setup(client, auth, low=0, high=100)
    conn, txn = await hold(
        engine, "UPDATE series SET max_value = 50 WHERE id = :i", i=series_id
    )  # a PUT in progress
    insert = asyncio.create_task(send(client, sensor["apiKey"], value=90, timestamp=T0))
    await asyncio.sleep(0.4)
    assert not insert.done()
    await release(conn, txn)
    response = await asyncio.wait_for(insert, timeout=5)
    assert response.status_code == 422  # judged against the committed new range
    assert await count(engine, "measurements") == 0


async def test_an_insert_waits_for_a_series_delete_and_then_is_401(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id, sensor = await setup(client, auth)
    conn, txn = await hold(engine, "DELETE FROM series WHERE id = :i", i=series_id)
    insert = asyncio.create_task(send(client, sensor["apiKey"], value=1, timestamp=T0))
    await asyncio.sleep(0.4)
    assert not insert.done()
    await release(conn, txn)
    assert (await asyncio.wait_for(insert, timeout=5)).status_code == 401  # no 500 / FK error


async def test_an_insert_waits_for_an_unregistration_and_then_is_401(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    _, sensor = await setup(client, auth)
    conn, txn = await hold(
        engine, "UPDATE sensors SET revoked_at = now() WHERE id = :i", i=sensor["id"]
    )
    insert = asyncio.create_task(send(client, sensor["apiKey"], value=1, timestamp=T0))
    await asyncio.sleep(0.4)
    assert not insert.done()
    await release(conn, txn)
    assert (await asyncio.wait_for(insert, timeout=5)).status_code == 401
    assert await count(engine, "measurements") == 0


async def test_a_range_change_waits_for_an_insert_in_flight_and_sees_its_row(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id, sensor = await setup(client, auth, low=0, high=100)
    # An insert that has locked the series (FOR SHARE) but not yet written its row:
    conn, txn = await hold(engine, "SELECT 1 FROM series WHERE id = :i FOR SHARE", i=series_id)
    shrink = asyncio.create_task(
        client.put(
            f"/api/series/{series_id}",
            json={"name": "PM2.5", "minValue": 0, "maxValue": 50, "color": "#1f77b4"},
            headers=auth,
        )
    )
    await asyncio.sleep(0.4)
    assert not shrink.done()
    await conn.execute(
        text(
            "INSERT INTO measurements (series_id, sensor_id, value, ts) VALUES (:s, :n, 90, now())"
        ),
        {"s": series_id, "n": sensor["id"]},
    )
    await release(conn, txn)
    response = await asyncio.wait_for(shrink, timeout=5)
    assert response.status_code == 409
    assert response.json()["dataMax"] == 90


async def test_many_parallel_inserts_from_one_sensor_all_succeed_without_deadlocks(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    _, sensor = await setup(client, auth)
    responses = await asyncio.gather(
        *[send(client, sensor["apiKey"], value=i % 100, timestamp=T0) for i in range(40)]
    )
    assert [r.status_code for r in responses] == [201] * 40
    assert len({r.json()["id"] for r in responses}) == 40
    assert await count(engine, "measurements") == 40


async def test_parallel_inserts_from_several_sensors_of_one_series(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id, first = await setup(client, auth)
    registered = await client.post(
        "/api/sensors", json={"name": "second", "seriesId": series_id}, headers=auth
    )
    second = registered.json()
    keys = [first["apiKey"], second["apiKey"]]
    responses = await asyncio.gather(
        *[send(client, keys[i % 2], value=i, timestamp=T1) for i in range(30)]
    )
    assert [r.status_code for r in responses] == [201] * 30
    assert await count(engine, "measurements") == 30


# --- reading: GET /api/measurements -----------------------------------------------------------


async def seed(
    client: httpx.AsyncClient, auth: dict[str, str], stamps: list[str], **kw: Any
) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    series_id, sensor = await setup(client, auth, **kw)
    created = []
    for i, stamp in enumerate(stamps):
        response = await send(client, sensor["apiKey"], value=i + 1, timestamp=stamp)
        assert response.status_code == 201
        created.append(response.json())
    return series_id, sensor, created


async def test_the_list_is_public_empty_by_default_and_has_a_total_count_header(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/measurements")
    assert (response.status_code, response.json()) == (200, [])
    assert response.headers["x-total-count"] == "0"
    assert response.headers["content-type"].startswith("application/json")


async def test_there_is_no_implicit_time_window_and_default_order_is_ascending(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    ancient = "2026-01-10T12:00:00Z"  # months before "now": must still be returned
    series_id, _, _ = await seed(client, auth, [ancient, T0, "2026-01-10T10:30:00Z", T1])
    response = await client.get(f"/api/measurements?series={series_id}")
    stamps = [m["timestamp"] for m in response.json()]
    assert stamps == [T0, "2026-01-10T10:30:00Z", T1, ancient]
    assert len(stamps) == 4
    assert response.headers["x-total-count"] == "4"


async def test_from_and_to_are_a_closed_interval(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series_id, _, _ = await seed(client, auth, [T0, T1, T2])
    base = f"/api/measurements?series={series_id}"
    both = (await client.get(f"{base}&from={T0}&to={T1}")).json()
    assert [m["timestamp"] for m in both] == [T0, T1]
    assert [m["timestamp"] for m in (await client.get(f"{base}&from={T1}")).json()] == [T1, T2]
    assert [m["timestamp"] for m in (await client.get(f"{base}&to={T1}")).json()] == [T0, T1]
    assert len((await client.get(f"{base}&from={T1}&to={T1}")).json()) == 1
    assert (await client.get(f"{base}&from=2026-01-10T11:00:01Z")).json()[0]["timestamp"] == T2
    # Offsets are honoured; a "+" that arrives as a space (unencoded) is repaired.
    shifted = (await client.get(f"{base}&from=2026-01-10T13:00:00%2B02:00&to={T1}")).json()
    assert [m["timestamp"] for m in shifted] == [T1]
    unencoded = (await client.get(f"{base}&from=2026-01-10T13:00:00+02:00&to={T1}")).json()
    assert [m["timestamp"] for m in unencoded] == [T1]


async def test_series_filter_accepts_one_many_or_no_ids(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    a, _, _ = await seed(client, auth, [T0, T1], name="A")
    b, _, _ = await seed(client, auth, [T2], name="B")
    c, _, _ = await seed(client, auth, [T0], name="C")
    assert len((await client.get(f"/api/measurements?series={a}")).json()) == 2
    both = (await client.get(f"/api/measurements?series={a},{b}")).json()
    assert len(both) == 3
    assert {m["seriesId"] for m in both} == {a, b}
    assert len((await client.get(f"/api/measurements?series={a}, {c}")).json()) == 3
    every = await client.get("/api/measurements")
    assert len(every.json()) == 4
    assert len((await client.get("/api/measurements?series=")).json()) == 4  # empty = all
    assert (await client.get("/api/measurements?series=999999")).json() == []  # unknown: empty
    assert len((await client.get(f"/api/measurements?series={a},{a}")).json()) == 2  # duplicates


async def test_sort_descending_and_stable_order_for_equal_timestamps(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series_id, _, _ = await seed(client, auth, [T1, T0, T1, T2])
    desc = (await client.get(f"/api/measurements?series={series_id}&sort=-timestamp")).json()
    assert [m["timestamp"] for m in desc] == [T2, T1, T1, T0]
    asc = (await client.get(f"/api/measurements?series={series_id}&sort=timestamp")).json()
    assert [m["timestamp"] for m in asc] == [T0, T1, T1, T2]
    ids_at_t1 = [m["id"] for m in asc if m["timestamp"] == T1]
    assert ids_at_t1 == sorted(ids_at_t1)  # ties broken by id, ascending
    desc_ids = [m["id"] for m in desc if m["timestamp"] == T1]
    assert desc_ids == sorted(desc_ids, reverse=True)


async def test_limit_default_maximum_and_total_count(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id, sensor = await setup(client, auth, high=2000)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO measurements (series_id, sensor_id, value, ts) "
                "SELECT :s, :n, g, "
                "'2026-01-01T00:00:00Z'::timestamptz + (g || ' minutes')::interval "
                "FROM generate_series(1, 1005) g"
            ),
            {"s": series_id, "n": sensor["id"]},
        )
    default = await client.get(f"/api/measurements?series={series_id}")
    assert len(default.json()) == 1000  # default limit
    assert default.headers["x-total-count"] == "1005"
    two = await client.get(f"/api/measurements?series={series_id}&limit=2&sort=-timestamp")
    assert [m["value"] for m in two.json()] == [1005, 1004]
    assert two.headers["x-total-count"] == "1005"
    everything = await client.get(f"/api/measurements?series={series_id}&limit=10000")
    assert len(everything.json()) == 1005


@pytest.mark.parametrize(
    "query",
    [
        "from=nie-data",
        "to=nie-data",
        "from=2026-01-10T10:00:00",
        "from=2026-01-10",
        "from=1768039200",
        "from=2026-01-10T11:00:00Z&to=2026-01-10T10:00:00Z",
        "sort=foo",
        "sort=-",
        "limit=0",
        "limit=-5",
        "limit=10001",
        "limit=abc",
        "limit=1.5",
        "series=a,b",
        "series=1,x",
        "series=1,,2",
        "series=0",
        "series=-1",
        "series=1.5",
        "series=9223372036854775808",
        "from=9999-12-31T23:59:59-12:00x",
    ],
)
async def test_bad_query_parameters_are_400_with_a_field_error(
    client: httpx.AsyncClient, query: str
) -> None:
    response = await client.get(f"/api/measurements?{query}")
    assert response.status_code == 400, query
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["errors"][0]["field"] in {"from", "to", "sort", "limit", "series"}


async def test_too_many_series_ids_is_400(client: httpx.AsyncClient) -> None:
    ids = ",".join(str(i) for i in range(1, 202))
    assert (await client.get(f"/api/measurements?series={ids}")).status_code == 400


async def test_extreme_but_valid_from_to_values_do_not_crash(client: httpx.AsyncClient) -> None:
    for query in (
        "from=9999-12-31T23:59:59-12:00",  # converts beyond year 9999
        "to=0001-01-01T00:00:00%2B14:00",
        "from=0001-01-01T00:00:00Z&to=9999-12-31T23:59:59Z",
    ):
        assert (await client.get(f"/api/measurements?{query}")).status_code in (200, 400), query


async def test_unknown_query_parameters_are_ignored_and_xml_is_406(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.get("/api/measurements?whatever=1")).status_code == 200
    response = await client.get("/api/measurements", headers={"Accept": "application/xml"})
    assert response.status_code == 406


async def test_reading_needs_no_authentication_and_leaks_no_secrets(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series_id, sensor, _ = await seed(client, auth, [T0])
    response = await client.get(f"/api/measurements?series={series_id}")
    assert response.status_code == 200
    assert sensor["apiKey"] not in response.text
    assert not FORBIDDEN_KEYS.search(response.text)
    assert set(response.json()[0]) == {"id", "seriesId", "sensorId", "value", "timestamp"}


# --- reading one, immutability ------------------------------------------------------------------


async def test_get_one_and_unknown_ids(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    _, _, created = await seed(client, auth, [T0])
    assert (await client.get(f"/api/measurements/{created[0]['id']}")).json() == created[0]
    for measurement_id in ("999999999", "0", "-1", "abc", "1.5", "9223372036854775808"):
        response = await client.get(f"/api/measurements/{measurement_id}")
        assert response.status_code == 404, measurement_id
        assert response.headers["content-type"].startswith("application/problem+json")


async def test_measurements_cannot_be_modified_or_deleted(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, sensor, created = await seed(client, auth, [T0])
    url = f"/api/measurements/{created[0]['id']}"
    key = {"X-API-Key": sensor["apiKey"]}
    for headers in (auth, key, {}):
        for method in ("PUT", "PATCH", "DELETE", "POST"):
            response = await client.request(method, url, json={"value": 99}, headers=headers)
            assert response.status_code == 405, (method, headers)
            assert response.headers["allow"] == "GET"
            assert response.headers["content-type"].startswith("application/problem+json")
    collection_delete = await client.delete("/api/measurements", headers=auth)
    assert collection_delete.status_code == 405
    assert collection_delete.headers["allow"] == "GET, POST"
    assert (await client.get(url)).json() == created[0]  # untouched


# --- integration with series -------------------------------------------------------------------


async def test_stats_and_the_range_check_see_ingested_data(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series_id, _, _ = await seed(client, auth, [T2, T0, T1])  # values 1, 2, 3
    stats = (await client.get(f"/api/series/{series_id}/stats")).json()
    assert (stats["count"], stats["min"], stats["max"]) == (3, 1, 3)
    assert stats["firstTimestamp"] == T0
    assert stats["lastTimestamp"] == T2
    shrink = await client.put(
        f"/api/series/{series_id}",
        json={"name": "PM2.5", "minValue": 2, "maxValue": 50, "color": "#1f77b4"},
        headers=auth,
    )
    assert shrink.status_code == 409
    assert (shrink.json()["dataMin"], shrink.json()["dataMax"]) == (1, 3)


# --- review follow-ups: count, rejection flood control, retention, clocks ------------------------


async def test_a_page_that_is_not_full_skips_the_count_query(
    app: FastAPI, client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series_id, _, _ = await seed(client, auth, [T0, T1, T2])
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement.lower())

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", record)
    try:
        small = await client.get(f"/api/measurements?series={series_id}")  # 3 rows, limit 1000
        counts_small = [s for s in statements if "count(" in s]
        statements.clear()
        full = await client.get(f"/api/measurements?series={series_id}&limit=2")  # page is full
        counts_full = [s for s in statements if "count(" in s]
        exact = await client.get(f"/api/measurements?series={series_id}&limit=3")  # full, == total
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", record)
    assert (len(small.json()), small.headers["x-total-count"]) == (3, "3")
    assert counts_small == []  # no count needed: that is everything
    assert (len(full.json()), full.headers["x-total-count"]) == (2, "3")
    assert len(counts_full) == 1
    assert (len(exact.json()), exact.headers["x-total-count"]) == (3, "3")


async def test_the_rejection_row_cap_is_per_sensor_and_excess_is_still_refused(
    app: FastAPI,
    client: httpx.AsyncClient,
    auth: dict[str, str],
    engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app.state.rejection_limiter = LoginThrottle(max_failures=3, window_seconds=60)
    series_id, noisy = await setup(client, auth, name="noisy")
    quiet = (
        await client.post(
            "/api/sensors", json={"name": "quiet", "seriesId": series_id}, headers=auth
        )
    ).json()
    with caplog.at_level(logging.DEBUG, logger="zai.measurements"):
        responses = [await send(client, noisy["apiKey"], value=999, timestamp=T0) for _ in range(8)]
        codes = [r.status_code for r in responses]
        other = await send(client, quiet["apiKey"], value=999, timestamp=T0)
    assert codes == [422] * 8  # every one is refused...
    assert other.status_code == 422
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT sensor_id, count(*) FROM measurement_rejections GROUP BY sensor_id")
            )
        ).all()
    assert {(r[0], r[1]) for r in rows} == {(noisy["id"], 3), (quiet["id"], 1)}  # ...3 rows, not 8
    records = [r for r in caplog.records if r.getMessage() == "measurement.rejected"]
    warnings = [r for r in records if r.levelno == logging.WARNING]
    debug = [r for r in records if r.levelno == logging.DEBUG]
    assert (len(warnings), len(debug)) == (4, 5)  # 3 noisy + 1 quiet recorded, 5 over the cap
    assert all(r.fields["recorded"] is False for r in debug)  # type: ignore[attr-defined]


async def test_old_rejections_are_purged_and_recent_ones_kept(
    app: FastAPI, client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    _, sensor = await setup(client, auth)
    for _ in range(3):
        await send(client, sensor["apiKey"], value=999, timestamp=T0)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE measurement_rejections SET created_at = now() - interval '31 days' "
                "WHERE id IN (SELECT min(id) FROM measurement_rejections)"
            )
        )
    async with app.state.session_factory() as session:
        assert await measurement_service.purge_old_rejections(session, 30) == 1
    assert await count(engine, "measurement_rejections") == 2
    async with app.state.session_factory() as session:
        assert await measurement_service.purge_old_rejections(session, 30) == 0


async def test_one_clock_is_used_for_receipt_and_last_seen(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    _, sensor = await setup(client, auth)
    before = datetime.now(UTC)
    await send(client, sensor["apiKey"], value=1, timestamp=T0)
    after = datetime.now(UTC)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT m.received_at, s.last_measurement_at "
                    "FROM measurements m JOIN sensors s ON s.id = m.sensor_id"
                )
            )
        ).one()
    assert row.received_at == row.last_measurement_at  # the same instant, not two clock reads
    # Compared with the host clock only: the database VM's clock may drift (e.g. after sleep).
    assert before <= row.received_at <= after
