"""``GET /api/rejections``: the admin error-log view (F4)."""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.test_measurements import send, setup

FORBIDDEN = re.compile(r"password|hash|salt|apiKey|zai_", re.I)


async def get(client: httpx.AsyncClient, auth: dict[str, str], query: str = "") -> httpx.Response:
    return await client.get(f"/api/rejections{query}", headers=auth)


# --- auth and parameters -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer forged-token"}, {"Authorization": "Basic YWRtaW46eA=="}],
)
async def test_it_is_admin_only(
    client: httpx.AsyncClient, admin: Any, headers: dict[str, str]
) -> None:
    response = await client.get("/api/rejections", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_a_sensor_key_is_not_an_admin_credential(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, sensor = await setup(client, auth)
    response = await client.get("/api/rejections", headers={"X-API-Key": sensor["apiKey"]})
    assert response.status_code == 401


async def test_authentication_comes_before_parameter_validation(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/rejections?limit=0")).status_code == 401


@pytest.mark.parametrize("limit", ["0", "-1", "1001", "abc", "1.5", ""])
async def test_a_bad_limit_is_a_400_problem(
    client: httpx.AsyncClient, auth: dict[str, str], limit: str
) -> None:
    response = await get(client, auth, f"?limit={limit}")
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["errors"][0]["field"] == "limit"
    assert limit not in response.json()["detail"] if limit else True  # the value is not echoed


async def test_a_non_json_accept_header_is_a_406(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.get("/api/rejections", headers={**auth, "Accept": "application/xml"})
    assert response.status_code == 406


# --- content -------------------------------------------------------------------------------------


async def test_it_is_empty_when_nothing_was_rejected(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    response = await get(client, auth)
    assert (response.status_code, response.json()) == (200, [])


async def test_a_rejected_measurement_appears_with_names_and_no_secrets(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series_id, sensor = await setup(client, auth, name="Lab PM2.5", low=0, high=100)
    assert (await send(client, sensor["apiKey"], value=612.5)).status_code == 422
    future = (datetime.now(UTC) + timedelta(hours=2)).replace(microsecond=0)
    assert (
        await send(client, sensor["apiKey"], value=5, timestamp=future.isoformat())
    ).status_code == 422

    response = await get(client, auth)
    assert response.status_code == 200
    items = response.json()
    assert [(i["reason"], i["value"]) for i in items] == [
        ("future_timestamp", 5.0),
        ("out_of_range", 612.5),
    ]  # newest first
    newest = items[0]
    assert set(newest) == {
        "id", "seriesId", "seriesName", "sensorId", "sensorName",
        "value", "timestamp", "reason", "createdAt",
    }  # fmt: skip
    assert newest["seriesId"] == series_id
    assert newest["seriesName"] == "Lab PM2.5"
    assert newest["sensorId"] == sensor["id"]
    assert newest["sensorName"] == sensor["name"]
    assert datetime.fromisoformat(newest["timestamp"]) == future
    assert newest["timestamp"].endswith("Z")
    assert newest["createdAt"].endswith("Z")
    assert not FORBIDDEN.search(response.text)
    assert sensor["apiKey"] not in response.text


async def test_rejections_of_an_unregistered_sensor_keep_its_name(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    _, sensor = await setup(client, auth)
    await send(client, sensor["apiKey"], value=9999)
    assert (await client.delete(f"/api/sensors/{sensor['id']}", headers=auth)).status_code == 204
    (item,) = (await get(client, auth)).json()
    assert (item["sensorId"], item["sensorName"]) == (sensor["id"], sensor["name"])


async def test_a_row_without_a_sensor_is_still_listed_without_sensor_fields(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id, _ = await setup(client, auth)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO measurement_rejections (series_id, sensor_id, value, ts, reason) "
                "VALUES (:s, NULL, 1, now(), 'out_of_range')"
            ),
            {"s": series_id},
        )
    (item,) = (await get(client, auth)).json()
    assert "sensorId" not in item
    assert "sensorName" not in item
    assert item["seriesId"] == series_id


async def test_deleting_a_series_removes_its_rejections(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    series_id, sensor = await setup(client, auth)
    await send(client, sensor["apiKey"], value=9999)
    assert len((await get(client, auth)).json()) == 1
    await client.delete(f"/api/series/{series_id}", headers=auth)
    assert (await get(client, auth)).json() == []


# --- limit and order -----------------------------------------------------------------------------


async def seed_rows(engine: AsyncEngine, series_id: int, count: int) -> None:
    """``count`` rows, value = n, created one second apart (n = 1 is the oldest)."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO measurement_rejections (series_id, value, ts, reason, created_at) "
                "SELECT :s, n, :ts, 'out_of_range', "
                "CAST(:base AS timestamptz) + n * interval '1 second' "
                "FROM generate_series(1, :n) AS n"
            ),
            {"s": series_id, "n": count, "ts": base, "base": base},
        )


async def test_default_limit_is_100_newest_first(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id, _ = await setup(client, auth)
    await seed_rows(engine, series_id, 130)
    items = (await get(client, auth)).json()
    assert len(items) == 100
    assert [i["value"] for i in items[:3]] == [130.0, 129.0, 128.0]
    assert items[-1]["value"] == 31.0


async def test_limit_bounds(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id, _ = await setup(client, auth)
    await seed_rows(engine, series_id, 1010)
    assert len((await get(client, auth, "?limit=1")).json()) == 1
    assert len((await get(client, auth, "?limit=7")).json()) == 7
    assert len((await get(client, auth, "?limit=1000")).json()) == 1000
    assert (await get(client, auth, "?limit=1")).json()[0]["value"] == 1010.0


async def test_rows_created_in_the_same_instant_are_ordered_by_id(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id, _ = await setup(client, auth)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO measurement_rejections (series_id, value, ts, reason, created_at) "
                "SELECT :s, n, now(), 'out_of_range', '2026-01-01Z' FROM generate_series(1, 5) n"
            ),
            {"s": series_id},
        )
    values = [i["value"] for i in (await get(client, auth)).json()]
    assert values == [5.0, 4.0, 3.0, 2.0, 1.0]
