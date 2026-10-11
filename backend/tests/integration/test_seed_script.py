"""The temporary seeder must fill the app *through the API only* (F11)."""

from datetime import UTC, datetime

import httpx

from scripts.seed_temp import SPECS, seed_sample_data
from tests.conftest import Admin


async def test_seeding_goes_through_the_api_and_is_repeatable(
    client: httpx.AsyncClient, admin: Admin
) -> None:
    now = datetime.now(UTC)
    lines: list[str] = []
    first = await seed_sample_data(
        client, admin.username, admin.password, days=2, now=now, log=lines.append
    )
    assert first == {spec.name: 48 for spec in SPECS}  # 2 days of hourly points per series

    series = (await client.get("/api/series")).json()
    assert [s["name"] for s in series] == [spec.name for spec in SPECS]
    for spec, item in zip(SPECS, series, strict=True):
        assert (item["minValue"], item["maxValue"]) == (spec.min_value, spec.max_value)
        assert (item["color"], item["icon"], item["unit"]) == (spec.color, spec.icon, spec.unit)
        listed = await client.get(f"/api/measurements?series={item['id']}&limit=10000")
        points = listed.json()
        assert len(points) == 48
        assert listed.headers["x-total-count"] == "48"
        assert all(spec.min_value <= p["value"] <= spec.max_value for p in points)
        assert all(p["sensorId"] is not None for p in points)  # every point came from a sensor key
        stamps = [datetime.fromisoformat(p["timestamp"]) for p in points]
        assert stamps == sorted(stamps)
        assert all(s <= now and s.minute == 0 and s.second == 0 for s in stamps)  # past, hourly

    login = await client.post(
        "/api/auth/login", json={"username": admin.username, "password": admin.password}
    )
    headers = {"Authorization": f"Bearer {login.json()['accessToken']}"}
    sensors = (await client.get("/api/sensors", headers=headers)).json()
    assert len(sensors) == len(SPECS)
    assert all(s["lastMeasurementAt"] is not None for s in sensors)

    second = await seed_sample_data(
        client, admin.username, admin.password, days=2, now=now, log=lines.append
    )
    assert set(second.values()) == {0}  # existing data is left alone: no duplicates
    assert len((await client.get("/api/measurements?limit=10000")).json()) == 48 * len(SPECS)
    assert len((await client.get("/api/sensors", headers=headers)).json()) == len(SPECS)
