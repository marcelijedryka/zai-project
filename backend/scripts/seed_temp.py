"""TEMPORARY sample-data seeder (F11), replaced by ``zai-sensor seed`` in Phase 2.

Everything goes through the public API, as the course requires: it logs in as the admin,
creates the series and one sensor per series, then backfills hourly values with
``POST /api/measurements`` using those sensors' keys. Nothing touches the database directly.

    ZAI_ADMIN_PASSWORD=... uv run python -m scripts.seed_temp --api-url http://localhost:8000

The password is read from the environment (or a hidden prompt), never from the command line.
"""

import argparse
import asyncio
import getpass
import math
import os
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx


@dataclass(frozen=True)
class SeriesSpec:
    name: str
    min_value: float
    max_value: float
    color: str
    icon: str
    unit: str
    base: float  # typical level
    swing: float  # daily amplitude
    noise: float
    peak_hour: float  # hour of day (UTC) of the daily maximum


SPECS = [
    SeriesSpec("Warsaw - Air temperature", -40, 50, "#1f77b4", "circle", "°C", 11, 6, 0.6, 15),
    SeriesSpec("Warsaw - PM2.5", 0, 500, "#d62728", "triangle", "µg/m³", 22, 9, 3, 7),
    SeriesSpec("Warsaw - PM10", 0, 600, "#2ca02c", "rect", "µg/m³", 30, 11, 4, 8),
    SeriesSpec("Warsaw - NO2", 0, 400, "#9467bd", "rectRot", "µg/m³", 28, 14, 3, 18),
    SeriesSpec(
        "Lab - Indoor temperature (synthetic)", 10, 35, "#8c564b", "star", "°C", 21, 3, 0.3, 16
    ),
]


def hourly_values(spec: SeriesSpec, hours: list[datetime], rng: random.Random) -> list[float]:
    """A daily sine plus noise, kept strictly inside the series range."""
    values = []
    for moment in hours:
        hour = moment.hour + moment.minute / 60
        wave = math.sin(2 * math.pi * (hour - spec.peak_hour + 6) / 24)
        value = spec.base + spec.swing * wave + rng.gauss(0, spec.noise)
        values.append(round(min(max(value, spec.min_value + 0.5), spec.max_value - 0.5), 2))
    return values


async def seed_sample_data(
    client: httpx.AsyncClient,
    username: str,
    password: str,
    *,
    days: int = 14,
    now: datetime | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    """Create the sample series and sensors if missing and backfill empty series.

    Returns ``{series name: measurements accepted this run}``. Series that already hold data
    are left alone, so running it twice does not duplicate points.
    """
    login = await client.post("/api/auth/login", json={"username": username, "password": password})
    login.raise_for_status()
    admin = {"Authorization": f"Bearer {login.json()['accessToken']}"}

    existing = {s["name"]: s for s in (await client.get("/api/series")).json()}
    end = (now or datetime.now(UTC)).replace(minute=0, second=0, microsecond=0)
    hours = [end - timedelta(hours=h) for h in range(days * 24, 0, -1)]  # oldest first, all past
    rng = random.Random(42)  # noqa: S311 - reproducible sample data, not a security use
    accepted: dict[str, int] = {}

    for spec in SPECS:
        series = existing.get(spec.name)
        if series is None:
            created = await client.post(
                "/api/series",
                headers=admin,
                json={
                    "name": spec.name,
                    "minValue": spec.min_value,
                    "maxValue": spec.max_value,
                    "color": spec.color,
                    "icon": spec.icon,
                    "unit": spec.unit,
                },
            )
            created.raise_for_status()
            series = created.json()
        stats = (await client.get(f"/api/series/{series['id']}/stats")).json()
        values = hourly_values(spec, hours, rng)  # drawn for every series: stable per series
        if stats["count"] > 0:
            log(f"  {spec.name}: already has {stats['count']} points, skipped")
            accepted[spec.name] = 0
            continue
        sensor = await client.post(
            "/api/sensors",
            headers=admin,
            json={"name": f"seed - {spec.name}", "seriesId": series["id"]},
        )
        sensor.raise_for_status()
        key = {"X-API-Key": sensor.json()["apiKey"]}
        count = 0
        for moment, value in zip(hours, values, strict=True):
            response = await client.post(
                "/api/measurements",
                headers=key,
                json={"value": value, "timestamp": moment.isoformat().replace("+00:00", "Z")},
            )
            response.raise_for_status()
            count += 1
        accepted[spec.name] = count
        log(f"  {spec.name}: {count} points sent")
    return accepted


async def _main(api_url: str, username: str, password: str, days: int) -> int:
    async with httpx.AsyncClient(base_url=api_url, timeout=30) as client:
        print(f"Seeding {api_url} (through the API only)")
        result = await seed_sample_data(client, username, password, days=days)
    print(f"Done: {sum(result.values())} measurements sent.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--api-url", default=os.environ.get("ZAI_API_URL", "http://localhost:8000"))
    parser.add_argument("--username", default="admin")
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args(argv)
    password = os.environ.get("ZAI_ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
    try:
        return asyncio.run(_main(args.api_url, args.username, password, args.days))
    except httpx.HTTPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
