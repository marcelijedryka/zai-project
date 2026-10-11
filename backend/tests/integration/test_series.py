"""Series CRUD, validation, range-conflict locking, stats and notifications (plan §5)."""

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings

T0 = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)
T1 = datetime(2026, 1, 10, 11, 0, tzinfo=UTC)
FORBIDDEN_KEYS = re.compile(r'"(password|passwordHash|hash|salt|apiKeyHash|keyHash)"\s*:', re.I)


def body(**over: Any) -> dict[str, Any]:
    return {"name": "Warsaw PM2.5", "minValue": 0, "maxValue": 100, "color": "#1f77b4"} | over


async def create(client: httpx.AsyncClient, auth: dict[str, str], **over: Any) -> dict[str, Any]:
    response = await client.post("/api/series", json=body(**over), headers=auth)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def add_measurement(engine: AsyncEngine, series_id: int, value: float, ts: datetime) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO measurements (series_id, value, ts) VALUES (:s, :v, :ts)"),
            {"s": series_id, "v": value, "ts": ts},
        )


# --- create ----------------------------------------------------------------------


async def test_create_returns_201_location_and_the_full_series(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    response = await client.post(
        "/api/series",
        json=body(unit="°C", icon="triangle", description="Hourly PM2.5"),
        headers=auth,
    )
    assert response.status_code == 201
    data = response.json()
    assert response.headers["location"] == f"/api/series/{data['id']}"
    assert isinstance(data["id"], int)
    assert {k: data[k] for k in ("name", "minValue", "maxValue", "color", "icon", "unit")} == {
        "name": "Warsaw PM2.5",
        "minValue": 0,
        "maxValue": 100,
        "color": "#1f77b4",
        "icon": "triangle",
        "unit": "°C",
    }
    assert data["description"] == "Hourly PM2.5"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+Z", data["createdAt"])
    assert data["updatedAt"] >= data["createdAt"]
    assert not FORBIDDEN_KEYS.search(response.text)
    fetched = await client.get(response.headers["location"])
    assert fetched.status_code == 200
    assert fetched.json() == data


async def test_optional_fields_default_to_null_and_names_are_trimmed(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    data = await create(client, auth, name="  Padded name  ")
    assert (data["icon"], data["unit"], data["description"]) == (None, None, None)
    assert data["name"] == "Padded name"


async def test_integers_floats_and_negative_ranges_are_accepted(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    data = await create(client, auth, minValue=-40, maxValue=50.5)
    assert (data["minValue"], data["maxValue"]) == (-40, 50.5)


@pytest.mark.parametrize(
    "override",
    [
        {"name": None},
        {"name": 5},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 101},
        {"minValue": "0"},
        {"minValue": True},
        {"maxValue": None},
        {"maxValue": [1]},
        {"color": "red"},
        {"color": "#12345"},
        {"color": "#1234567"},
        {"color": "#12345g"},
        {"color": "#aabbcc\n"},
        {"color": None},
        {"icon": 5},
        {"icon": "x" * 51},
        {"unit": "x" * 21},
        {"description": "x" * 1001},
    ],
)
async def test_invalid_create_bodies_are_400(
    client: httpx.AsyncClient, auth: dict[str, str], override: dict[str, Any]
) -> None:
    response = await client.post("/api/series", json=body(**override), headers=auth)
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["errors"]


@pytest.mark.parametrize("missing", ["name", "minValue", "maxValue", "color"])
async def test_missing_required_fields_are_400(
    client: httpx.AsyncClient, auth: dict[str, str], missing: str
) -> None:
    payload = body()
    del payload[missing]
    response = await client.post("/api/series", json=payload, headers=auth)
    assert response.status_code == 400
    assert response.json()["errors"][0]["field"] == missing


@pytest.mark.parametrize(
    "raw",
    [
        '{"name":"n","minValue":NaN,"maxValue":1,"color":"#000000"}',
        '{"name":"n","minValue":0,"maxValue":Infinity,"color":"#000000"}',
        '{"name":"n","minValue":0,"maxValue":1e999,"color":"#000000"}',
        '{"name":',
        "[]",
        "null",
    ],
)
async def test_non_finite_numbers_and_malformed_json_are_400(
    client: httpx.AsyncClient, auth: dict[str, str], raw: str
) -> None:
    response = await client.post(
        "/api/series", content=raw, headers={**auth, "Content-Type": "application/json"}
    )
    assert response.status_code == 400


@pytest.mark.parametrize(("low", "high"), [(10, 10), (10, 1), (0, 0), (-1, -1)])
async def test_min_not_below_max_is_422(
    client: httpx.AsyncClient, auth: dict[str, str], low: float, high: float
) -> None:
    response = await client.post(
        "/api/series", json=body(minValue=low, maxValue=high), headers=auth
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "urn:zai:problem:invalid-range"


async def test_duplicate_names_are_409_case_insensitively(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    await create(client, auth, name="Warsaw NO2")
    response = await client.post("/api/series", json=body(name="warsaw no2"), headers=auth)
    assert response.status_code == 409
    assert response.json()["type"] == "urn:zai:problem:duplicate-name"


async def test_concurrent_creates_of_the_same_name_yield_one_201_and_the_rest_409(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    """The pre-check can lose a race; the unique index must still map to 409, never 500."""
    responses = await asyncio.gather(
        *[client.post("/api/series", json=body(name="Racing"), headers=auth) for _ in range(6)]
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [201, 409, 409, 409, 409, 409]
    assert len((await client.get("/api/series")).json()) == 1


async def test_concurrent_renames_to_the_same_name_never_500(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    ids = [(await create(client, auth, name=f"Series {i}"))["id"] for i in range(5)]
    responses = await asyncio.gather(
        *[client.put(f"/api/series/{i}", json=body(name="Same target"), headers=auth) for i in ids]
    )
    assert sorted(r.status_code for r in responses) == [200, 409, 409, 409, 409]


async def test_create_requires_json_and_authentication(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    assert (await client.post("/api/series", json=body())).status_code == 401
    wrong_type = await client.post(
        "/api/series", content="name=x", headers={**auth, "Content-Type": "text/plain"}
    )
    assert wrong_type.status_code == 415


# --- read --------------------------------------------------------------------------


async def test_list_is_public_ordered_by_id_and_empty_by_default(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    empty = await client.get("/api/series")
    assert (empty.status_code, empty.json()) == (200, [])
    assert empty.headers["content-type"].startswith("application/json")
    first = await create(client, auth, name="B series")
    second = await create(client, auth, name="A series")
    listed = (await client.get("/api/series")).json()
    assert [s["id"] for s in listed] == [first["id"], second["id"]]
    xml = await client.get("/api/series", headers={"Accept": "application/xml"})
    assert xml.status_code == 406


@pytest.mark.parametrize(
    "series_id",
    [
        "999999999",
        "0",
        "-1",
        "abc",
        "1.5",
        "1e3",
        "99999999999999999999",
        "9223372036854775807",
        "9223372036854775808",
    ],
)
async def test_unknown_or_unparseable_ids_are_404_problems(
    client: httpx.AsyncClient, series_id: str
) -> None:
    for suffix in ("", "/stats"):
        response = await client.get(f"/api/series/{series_id}{suffix}")
        assert response.status_code == 404, (series_id, suffix)
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["title"] == "Not Found"


async def test_wrong_methods_are_405(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    created = await create(client, auth)
    response = await client.patch(f"/api/series/{created['id']}", json={}, headers=auth)
    assert response.status_code == 405
    assert response.headers["allow"] == "DELETE, GET, HEAD, PUT"
    collection = await client.delete("/api/series", headers=auth)
    assert (collection.status_code, collection.headers["allow"]) == (405, "GET, HEAD, POST")


# --- update ------------------------------------------------------------------------


async def test_put_replaces_everything_and_nulls_omitted_optionals(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    created = await create(client, auth, unit="°C", icon="star", description="old")
    response = await client.put(
        f"/api/series/{created['id']}",
        json=body(name="Renamed", color="#ff7f0e", minValue=-5, maxValue=5),
        headers=auth,
    )
    assert response.status_code == 200
    data = response.json()
    assert (data["name"], data["color"], data["minValue"], data["maxValue"]) == (
        "Renamed",
        "#ff7f0e",
        -5,
        5,
    )
    assert (data["icon"], data["unit"], data["description"]) == (None, None, None)
    assert data["id"] == created["id"]
    assert data["createdAt"] == created["createdAt"]
    assert data["updatedAt"] > created["updatedAt"]
    assert (await client.get(f"/api/series/{created['id']}")).json() == data


async def test_put_keeping_the_own_name_is_fine_but_taking_anothers_is_409(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    first = await create(client, auth, name="First")
    second = await create(client, auth, name="Second")
    same = await client.put(f"/api/series/{first['id']}", json=body(name="FIRST"), headers=auth)
    assert same.status_code == 200
    clash = await client.put(f"/api/series/{second['id']}", json=body(name="first"), headers=auth)
    assert clash.status_code == 409


async def test_put_status_precedence(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    created = await create(client, auth)
    url = f"/api/series/{created['id']}"
    # 401 first, even for a missing id.
    assert (await client.put("/api/series/999999", json=body())).status_code == 401
    # 400 (invalid body) before 404.
    assert (await client.put("/api/series/999999", json={"x": 1}, headers=auth)).status_code == 400
    # 404 before 422.
    bad_range = body(minValue=5, maxValue=1)
    assert (await client.put("/api/series/999999", json=bad_range, headers=auth)).status_code == 404
    assert (await client.put(url, json=bad_range, headers=auth)).status_code == 422
    # A bad path id is a 404 even with a valid body.
    assert (await client.put("/api/series/abc", json=body(), headers=auth)).status_code == 404


async def test_range_that_excludes_stored_data_is_409_with_the_data_bounds(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    created = await create(client, auth, minValue=0, maxValue=100)
    await add_measurement(engine, created["id"], 5.5, T0)
    await add_measurement(engine, created["id"], 30, T1)
    url = f"/api/series/{created['id']}"
    for low, high in [(50, 60), (0, 20), (10, 100), (6, 100)]:
        response = await client.put(url, json=body(minValue=low, maxValue=high), headers=auth)
        assert response.status_code == 409, (low, high)
        problem = response.json()
        assert problem["type"] == "urn:zai:problem:range-conflict"
        assert (problem["dataMin"], problem["dataMax"]) == (5.5, 30)
        assert response.headers["content-type"].startswith("application/problem+json")
    unchanged = (await client.get(url)).json()
    assert (unchanged["minValue"], unchanged["maxValue"]) == (0, 100)


async def test_range_touching_the_stored_data_exactly_is_allowed(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    created = await create(client, auth)
    await add_measurement(engine, created["id"], 5, T0)
    await add_measurement(engine, created["id"], 30, T1)
    url = f"/api/series/{created['id']}"
    tight = await client.put(url, json=body(minValue=5, maxValue=30), headers=auth)
    assert tight.status_code == 200
    wider = await client.put(url, json=body(minValue=-100, maxValue=1000), headers=auth)
    assert wider.status_code == 200


async def test_a_series_without_data_can_be_shrunk_freely(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    created = await create(client, auth)
    response = await client.put(
        f"/api/series/{created['id']}", json=body(minValue=1, maxValue=2), headers=auth
    )
    assert response.status_code == 200


async def test_a_measurement_being_inserted_cannot_slip_past_a_range_shrink(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    """Inserts hold ``FOR SHARE`` on the series row; the shrink needs ``FOR UPDATE``, so it must
    wait for the insert to commit and then see the new row (409), not pass the check first."""
    created = await create(client, auth, minValue=0, maxValue=100)
    series_id = created["id"]
    insert = await engine.connect()
    transaction = await insert.begin()
    await insert.execute(text("SELECT 1 FROM series WHERE id = :i FOR SHARE"), {"i": series_id})
    shrink = asyncio.create_task(
        client.put(f"/api/series/{series_id}", json=body(minValue=0, maxValue=50), headers=auth)
    )
    await asyncio.sleep(0.4)
    assert not shrink.done()  # blocked behind the in-flight insert
    await insert.execute(
        text("INSERT INTO measurements (series_id, value, ts) VALUES (:s, 90, :ts)"),
        {"s": series_id, "ts": T0},
    )
    await transaction.commit()
    await insert.close()
    response = await asyncio.wait_for(shrink, timeout=5)
    assert response.status_code == 409
    assert response.json()["dataMax"] == 90


# --- delete ------------------------------------------------------------------------


async def test_delete_removes_the_series_with_its_sensors_and_measurements(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    created = await create(client, auth)
    series_id = created["id"]
    await add_measurement(engine, series_id, 1, T0)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO sensors (series_id, name, key_hash) VALUES (:s, 's', :h)"),
            {"s": series_id, "h": "a" * 64},
        )
    response = await client.delete(f"/api/series/{series_id}", headers=auth)
    assert (response.status_code, response.content) == (204, b"")
    assert (await client.get(f"/api/series/{series_id}")).status_code == 404
    async with engine.connect() as conn:
        for table in ("sensors", "measurements"):
            count: int = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
            assert count == 0, table
    assert (await client.delete(f"/api/series/{series_id}", headers=auth)).status_code == 404


async def test_delete_without_a_token_is_401_even_for_missing_ids(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    created = await create(client, auth)
    assert (await client.delete(f"/api/series/{created['id']}")).status_code == 401
    assert (await client.delete("/api/series/999999")).status_code == 401
    assert (await client.delete("/api/series/abc")).status_code == 401
    assert (await client.get(f"/api/series/{created['id']}")).status_code == 200


async def test_cookie_auth_on_series_writes_needs_an_allowed_origin(
    client: httpx.AsyncClient, token: str
) -> None:
    cookie = {"Cookie": f"__Host-zai_session={token}"}
    assert (await client.post("/api/series", json=body(), headers=cookie)).status_code == 403
    allowed = {**cookie, "Origin": "http://localhost:5173"}
    assert (await client.post("/api/series", json=body(), headers=allowed)).status_code == 201


# --- stats -------------------------------------------------------------------------


async def test_stats_of_an_empty_series(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    created = await create(client, auth)
    response = await client.get(f"/api/series/{created['id']}/stats")
    assert response.status_code == 200
    assert response.json() == {
        "count": 0,
        "min": None,
        "max": None,
        "firstTimestamp": None,
        "lastTimestamp": None,
    }


async def test_stats_summarise_stored_measurements_only_for_that_series(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    mine = await create(client, auth, name="mine")
    other = await create(client, auth, name="other")
    await add_measurement(engine, mine["id"], 4.5, T1)
    await add_measurement(engine, mine["id"], -2, T0)
    await add_measurement(engine, other["id"], 99, T0)
    stats = (await client.get(f"/api/series/{mine['id']}/stats")).json()
    assert stats["count"] == 2
    assert (stats["min"], stats["max"]) == (-2, 4.5)
    assert stats["firstTimestamp"].startswith("2026-01-10T10:00:00")
    assert stats["lastTimestamp"].startswith("2026-01-10T11:00:00")
    assert stats["firstTimestamp"].endswith("Z")


# --- notifications -----------------------------------------------------------------


async def test_series_changes_are_announced_on_commit(
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
        "zai_series", lambda _c, _pid, _ch, payload: received.append(json.loads(payload))
    )
    try:
        created = await create(client, auth)
        await client.put(f"/api/series/{created['id']}", json=body(name="Renamed"), headers=auth)
        await client.delete(f"/api/series/{created['id']}", headers=auth)
        # Rolled-back or rejected operations must not announce anything.
        await client.put(f"/api/series/{created['id']}", json=body(name="Gone"), headers=auth)
        await client.post("/api/series", json=body(minValue=5, maxValue=1), headers=auth)
        for _ in range(50):
            if len(received) >= 3:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
    finally:
        await listener.close()
    assert [(e["action"], e["id"]) for e in received] == [
        ("created", created["id"]),
        ("updated", created["id"]),
        ("deleted", created["id"]),
    ]
    assert all(e["event"] == "series-changed" for e in received)
