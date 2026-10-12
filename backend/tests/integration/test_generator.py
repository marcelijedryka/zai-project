"""The ``zai-sensor`` CLI against the real application (real HTTP, real Timescale database).

The ``server`` fixture (tests/conftest.py) runs the app in a background thread on a free port
(the generator is a plain synchronous HTTP client). Covers F11 (`seed`), F13
(`backfill`, `live`, `send`) and F4 (the rejection demo) end to end.
"""

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import Admin
from tests.generator_utils import SEED_FILE, generator, seed


async def list_series(url: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(base_url=url) as client:
        return (await client.get("/api/series")).json()  # type: ignore[no-any-return]


async def points(url: str, series_id: int) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(base_url=url) as client:
        response = await client.get(f"/api/measurements?series={series_id}&limit=10000")
        return response.json()  # type: ignore[no-any-return]


async def test_seed_fills_the_app_through_the_api_and_is_repeatable(
    server: str, admin: Admin, tmp_path: Path
) -> None:
    keys_file = tmp_path / "keys.json"
    result = await seed(server, admin, "--save-keys", str(keys_file), days=14)
    assert result.exit_code == 0, result.output
    assert "sent=1680 accepted=1680 rejected=0 failed=0" in result.stdout

    series = await list_series(server)
    assert len(series) == 5
    assert not any(s["name"].upper().startswith("ZAI-TEST-") for s in series)  # F11 / pitfall 10
    assert {s["description"] for s in series} == {
        "Synthetic sample data"
    }  # never passed off as real
    now = datetime.now(UTC)
    for item in series:
        data = await points(server, item["id"])
        assert len(data) == 14 * 24  # F11 needs >= 15
        stamps = [datetime.fromisoformat(p["timestamp"]) for p in data]
        assert stamps == sorted(stamps)
        assert all(s <= now and s.minute == 0 for s in stamps)  # past, hourly
        assert all(p["sensorId"] is not None for p in data)  # every point came with a sensor key
        assert all(item["minValue"] <= p["value"] <= item["maxValue"] for p in data)

    # The keys stay in memory unless asked for; the file is private to its owner.
    assert stat.S_IMODE(keys_file.stat().st_mode) == 0o600
    saved = json.loads(keys_file.read_text(encoding="utf-8"))
    assert set(saved) == {s["name"] for s in series}

    again = await seed(server, admin, days=14)
    assert again.exit_code == 0, again.output
    assert "already up to date, skipped" in again.stdout
    assert len(await list_series(server)) == 5
    for item in series:
        assert len(await points(server, item["id"])) == 14 * 24  # nothing duplicated


async def test_seed_without_save_keys_leaves_no_usable_sensor_behind(
    server: str, admin: Admin
) -> None:
    result = await seed(server, admin)
    assert result.exit_code == 0, result.output
    assert "Unregistered 5 seed sensors" in result.stdout
    async with httpx.AsyncClient(base_url=server) as client:
        login = await client.post(
            "/api/auth/login", json={"username": admin.username, "password": admin.password}
        )
        headers = {"Authorization": f"Bearer {login.json()['accessToken']}"}
        assert (await client.get("/api/sensors", headers=headers)).json() == []
    series = await list_series(server)
    data = await points(server, series[0]["id"])
    assert len(data) == 2 * 24
    assert all(p["sensorId"] is not None for p in data)  # provenance survives the unregistering


async def test_seed_logs_out_and_rejects_a_wrong_admin_password(server: str, admin: Admin) -> None:
    wrong = await generator(
        server,
        "seed", "--config", str(SEED_FILE), "--admin-user", admin.username,
        env={"ZAI_ADMIN_PASSWORD": "not the password"},
    )  # fmt: skip
    assert wrong.exit_code == 3
    assert "not the password" not in wrong.output
    assert await list_series(server) == []


async def test_send_backfill_live_and_the_rejection_demo(
    server: str, admin: Admin, engine: AsyncEngine, tmp_path: Path
) -> None:
    keys_file = tmp_path / "keys.json"
    assert (await seed(server, admin, "--save-keys", str(keys_file))).exit_code == 0
    series = {s["name"]: s for s in await list_series(server)}
    lab = series["Lab – Indoor temperature (synthetic)"]
    key = json.loads(keys_file.read_text(encoding="utf-8"))[lab["name"]]
    env = {"ZAI_API_KEY": key}
    before = len(await points(server, lab["id"]))

    # backfill with past timestamps (20 days ago: outside the seeded 14 days)
    end = (datetime.now(UTC) - timedelta(days=20)).replace(microsecond=0)
    result = await generator(
        server,
        "backfill", "--count", "5", "--end", end.isoformat(), "--min", "10", "--max", "35",
        "--pattern", "random-walk", "--seed", "3",
        env=env,
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "✔" in result.stdout
    assert "°C" in result.stdout  # the unit is looked up from the series
    assert "sent=5 accepted=5 rejected=0 failed=0" in result.stdout
    data = await points(server, lab["id"])
    assert len(data) == before + 5
    assert datetime.fromisoformat(data[0]["timestamp"]) == end - timedelta(hours=4)

    # F4 demo: an out-of-range value is a red 422 line, nothing is stored, and it is logged
    rejected = await generator(server, "send", "--value", "999", env=env)
    assert rejected.exit_code == 1
    assert "✘ REJECTED (422)" in rejected.stdout
    assert len(await points(server, lab["id"])) == before + 5
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT value, reason FROM measurement_rejections"))).all()
    assert [(r.value, r.reason) for r in rows] == [(999.0, "out_of_range")]

    # --out-of-range-every: the run continues past every rejection
    mixed = await generator(
        server,
        "backfill", "--count", "6", "--end", end.isoformat(), "--min", "10", "--max", "35",
        "--pattern", "uniform", "--seed", "1", "--out-of-range-every", "3",
        env=env,
    )  # fmt: skip
    assert mixed.exit_code == 1
    assert "sent=6 accepted=4 rejected=2 failed=0" in mixed.stdout

    # live: server timestamps, one value per interval
    live = await generator(
        server, "live", "--interval", "1s", "--count", "2", "--min", "10", "--max", "35", env=env
    )
    assert live.exit_code == 0, live.output
    assert "sent=2 accepted=2" in live.stdout
    newest = (await points(server, lab["id"]))[-1]
    assert abs(datetime.now(UTC) - datetime.fromisoformat(newest["timestamp"])).seconds < 30


async def test_an_unregistered_sensor_is_told_so_with_exit_code_2(
    server: str, admin: Admin, tmp_path: Path
) -> None:
    keys_file = tmp_path / "keys.json"
    assert (await seed(server, admin, "--save-keys", str(keys_file))).exit_code == 0
    name = "Lab – Indoor temperature (synthetic)"
    key = json.loads(keys_file.read_text(encoding="utf-8"))[name]
    async with httpx.AsyncClient(base_url=server) as client:
        login = await client.post(
            "/api/auth/login", json={"username": admin.username, "password": admin.password}
        )
        headers = {"Authorization": f"Bearer {login.json()['accessToken']}"}
        sensors = (await client.get("/api/sensors", headers=headers)).json()
        lab = next(s for s in sensors if name in s["name"])
        assert (
            await client.delete(f"/api/sensors/{lab['id']}", headers=headers)
        ).status_code == 204

    result = await generator(server, "send", "--value", "20", env={"ZAI_API_KEY": key})
    assert result.exit_code == 2
    assert "✘ Sensor key rejected – sensor unregistered or key invalid" in result.stdout
    assert key not in result.output


async def test_backfill_from_open_meteo_sends_real_shaped_data_through_the_api(
    server: str, admin: Admin, tmp_path: Path
) -> None:
    keys_file = tmp_path / "keys.json"
    assert (await seed(server, admin, "--save-keys", str(keys_file))).exit_code == 0
    name = "Warsaw – Air temperature"
    key = json.loads(keys_file.read_text(encoding="utf-8"))[name]
    series = next(s for s in await list_series(server) if s["name"] == name)
    before = len(await points(server, series["id"]))

    start = (datetime.now(UTC) - timedelta(days=40)).replace(minute=0, second=0, microsecond=0)
    times = [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(6)]
    values: list[float | None] = [3.5, None, 4.5, 5.0, None, 6.25]  # naive times, nulls
    with respx.mock(assert_all_called=False) as mocked:
        mocked.route(host="127.0.0.1").pass_through()  # the app itself stays real
        mocked.get("https://api.open-meteo.com/v1/forecast").mock(
            return_value=httpx.Response(
                200, json={"hourly": {"time": times, "temperature_2m": values}}
            )
        )
        result = await generator(
            server,
            "backfill", "--source", "open-meteo", "--variable", "temperature_2m",
            "--start", start.isoformat(),
            env={"ZAI_API_KEY": key},
        )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "sent=4 accepted=4 rejected=0 failed=0" in result.stdout
    data = await points(server, series["id"])
    assert len(data) == before + 4
    assert [p["value"] for p in data[:4]] == [3.5, 4.5, 5.0, 6.25]
    assert [p["timestamp"] for p in data[:2]] == [
        start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        (start + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    ]  # naive Open-Meteo times arrived as UTC
