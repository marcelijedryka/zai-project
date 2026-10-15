"""``GET /api/measurements/aggregate`` and the ``measurements_hourly`` continuous aggregate."""

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration.test_measurements import setup


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 3, day, hour, minute, tzinfo=UTC)


async def refresh(engine: AsyncEngine) -> None:
    """Materialise now, like the policy does every minute (cannot run in a transaction)."""
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(
            text("CALL refresh_continuous_aggregate('measurements_hourly', NULL, NULL)")
        )


async def put(
    engine: AsyncEngine, series_id: int, *points: tuple[datetime, float], refreshed: bool = True
) -> None:
    """Rows straight into the hypertable (the aggregate does not care how they got there).

    Rows older than the aggregate's watermark only show up after a refresh, as in production
    (backfilled data waits for the next policy run); ``refreshed`` runs it for the test.
    """
    async with engine.begin() as conn:
        for when, value in points:
            await conn.execute(
                text("INSERT INTO measurements (series_id, ts, value) VALUES (:s, :t, :v)"),
                {"s": series_id, "t": when, "v": value},
            )
    if refreshed:
        await refresh(engine)


async def agg(client: httpx.AsyncClient, query: str = "") -> list[dict[str, Any]]:
    response = await client.get(f"/api/measurements/aggregate{query}")
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


# --- results -------------------------------------------------------------------------------------


async def test_hourly_buckets_with_avg_min_max_count(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id, _ = await setup(client, auth, high=1000)
    await put(
        engine,
        series_id,
        (at(1, 10, 5), 10),
        (at(1, 10, 25), 20),
        (at(1, 10, 55), 60),
        (at(1, 12, 0), 7),  # an empty hour in between yields no row
    )
    ten, twelve = await agg(client)
    assert ten == {
        "seriesId": series_id, "bucket": "2026-03-01T10:00:00Z",
        "avg": 30.0, "min": 10.0, "max": 60.0, "count": 3,
    }  # fmt: skip
    assert twelve == {
        "seriesId": series_id, "bucket": "2026-03-01T12:00:00Z",
        "avg": 7.0, "min": 7.0, "max": 7.0, "count": 1,
    }  # fmt: skip


async def test_daily_buckets_roll_up_with_a_correct_average(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id, _ = await setup(client, auth, high=1000)
    await put(
        engine,
        series_id,
        (at(1, 8), 0),  # hour 1: one value
        (at(1, 9, 10), 10),  # hour 2: three values
        (at(1, 9, 20), 10),
        (at(1, 9, 30), 10),
        (at(2, 0, 0), 100),  # the next UTC day
    )
    day1, day2 = await agg(client, "?bucket=1d")
    assert day1 == {
        "seriesId": series_id, "bucket": "2026-03-01T00:00:00Z",
        "avg": 7.5, "min": 0.0, "max": 10.0, "count": 4,
    }  # fmt: skip
    # An average of the hourly averages would be (0 + 10) / 2 = 5: the roll-up must use sum / count.
    assert day2["bucket"] == "2026-03-02T00:00:00Z"
    assert day2["avg"] == 100.0


async def test_series_filter_and_ordering(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    a, _ = await setup(client, auth, name="A", high=1000)
    b, _ = await setup(client, auth, name="B", high=1000)
    await put(engine, a, (at(1, 11), 1), (at(1, 9), 1))
    await put(engine, b, (at(1, 9), 2), (at(1, 10), 2))
    everything = await agg(client)
    assert [(r["bucket"][11:13], r["seriesId"]) for r in everything] == [
        ("09", a), ("09", b), ("10", b), ("11", a),
    ]  # fmt: skip
    assert [r["seriesId"] for r in await agg(client, f"?series={b}")] == [b, b]
    assert await agg(client, "?series=999999") == []
    assert len(await agg(client, f"?series={a},{b}")) == 4


async def test_from_and_to_select_the_overlapping_buckets(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id, _ = await setup(client, auth, high=1000)
    await put(engine, series_id, (at(1, 10, 30), 1), (at(1, 11, 30), 2), (at(1, 12, 15), 3))

    def hours(rows: list[dict[str, Any]]) -> list[str]:
        return [r["bucket"][11:13] for r in rows]

    # 10:45 lies inside the 10:00 bucket, so that bucket is part of the answer.
    assert hours(await agg(client, "?from=2026-03-01T10:45:00Z")) == ["10", "11", "12"]
    assert hours(await agg(client, "?from=2026-03-01T11:00:00Z")) == ["11", "12"]
    assert hours(await agg(client, "?to=2026-03-01T11:59:59Z")) == ["10", "11"]
    assert hours(await agg(client, "?to=2026-03-01T12:00:00Z")) == ["10", "11", "12"]  # closed
    assert hours(
        await agg(client, "?from=2026-03-01T11:10:00%2B00:00&to=2026-03-01T11:20:00Z")
    ) == ["11"]
    # An offset is converted: 13:30+02:00 is 11:30Z.
    assert hours(
        await agg(client, "?from=2026-03-01T13:30:00%2B02:00&to=2026-03-01T13:30:00%2B02:00")
    ) == ["11"]
    assert await agg(client, "?from=2027-01-01T00:00:00Z") == []


async def test_daily_range_starts_at_the_day_containing_from(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id, _ = await setup(client, auth, high=1000)
    await put(engine, series_id, (at(1, 3), 1), (at(1, 22), 3), (at(2, 5), 5))
    rows = await agg(client, "?bucket=1d&from=2026-03-01T23:00:00Z")
    assert [(r["bucket"][:10], r["count"]) for r in rows] == [("2026-03-01", 2), ("2026-03-02", 1)]


async def test_a_daily_bucket_is_whole_at_both_ends_of_the_range(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id, _ = await setup(client, auth, high=1000)
    await put(
        engine,
        series_id,
        (at(1, 23), 1),
        (at(2, 3), 2),
        (at(2, 8), 4),
        (at(2, 15), 6),  # after ``to`` below, but in the same day
        (at(2, 22), 8),
        (at(3, 0), 100),  # the next day, starting exactly at midnight
    )
    # ``to`` in the middle of March 2: the last day must still carry the whole day, like the first.
    rows = await agg(client, "?bucket=1d&from=2026-03-01T12:00:00Z&to=2026-03-02T10:30:00Z")
    assert [(r["bucket"][:10], r["count"], r["min"], r["max"], r["avg"]) for r in rows] == [
        ("2026-03-01", 1, 1.0, 1.0, 1.0),
        ("2026-03-02", 4, 2.0, 8.0, 5.0),
    ]
    # ``to`` exactly at midnight belongs to the next bucket (the interval is closed).
    rows = await agg(client, "?bucket=1d&to=2026-03-03T00:00:00Z")
    assert [r["bucket"][:10] for r in rows] == ["2026-03-01", "2026-03-02", "2026-03-03"]
    rows = await agg(client, "?bucket=1d&to=2026-03-02T23:59:59Z")
    assert [r["bucket"][:10] for r in rows] == ["2026-03-01", "2026-03-02"]
    # Hourly buckets are unchanged by this: ``to`` mid-hour includes that hour only.
    rows = await agg(client, "?to=2026-03-02T08:30:00Z")
    assert [r["bucket"][8:13] for r in rows] == ["01T23", "02T03", "02T08"]


async def test_a_to_at_the_end_of_the_calendar_is_no_problem(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id, _ = await setup(client, auth, high=1000)
    await put(engine, series_id, (at(1, 10), 5))
    for bucket in ("1h", "1d"):
        rows = await agg(client, f"?bucket={bucket}&to=9999-12-31T23:59:59Z")
        assert [r["count"] for r in rows] == [1]


async def test_limit_and_total_count(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    series_id, _ = await setup(client, auth, high=1000)
    await put(engine, series_id, *[(at(1, h), float(h)) for h in range(5)])
    cut = await client.get("/api/measurements/aggregate?limit=2")
    assert [r["bucket"][11:13] for r in cut.json()] == ["00", "01"]  # the oldest buckets
    assert cut.headers["x-total-count"] == "5"
    whole = await client.get("/api/measurements/aggregate?limit=5")
    assert (len(whole.json()), whole.headers["x-total-count"]) == (5, "5")
    assert (await client.get("/api/measurements/aggregate?bucket=1d")).headers[
        "x-total-count"
    ] == "1"


async def test_empty(client: httpx.AsyncClient) -> None:
    assert await agg(client) == []


# --- real-time aggregation, refresh, deleted series ----------------------------------------------


async def test_the_aggregate_is_real_time_but_old_data_waits_for_a_refresh(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    async with engine.connect() as conn:
        flag = await conn.execute(
            text(
                "SELECT materialized_only FROM timescaledb_information.continuous_aggregates "
                "WHERE view_name = 'measurements_hourly'"
            )
        )
    assert flag.scalar_one() is False  # real-time aggregation is explicitly on

    series_id, _ = await setup(client, auth, high=1000)
    future = datetime(2031, 1, 1, 10, tzinfo=UTC)  # newer than anything another test materialised
    await put(engine, series_id, (future, 4), refreshed=False)
    assert [r["count"] for r in await agg(client)] == [1]  # visible with no refresh at all
    await refresh(engine)
    assert [r["count"] for r in await agg(client)] == [1]  # materialised: same answer

    # The watermark is now past 2031-01-01 10:00: a row behind it is not in the materialised part
    # (backfilled history appears at the next policy run, at most a minute later) ...
    older = datetime(2030, 12, 31, 10, tzinfo=UTC)
    await put(engine, series_id, (older, 8), refreshed=False)
    assert [r["count"] for r in await agg(client)] == [1]
    await refresh(engine)  # ... which is what the refresh policy does
    assert [(r["bucket"][:10], r["count"]) for r in await agg(client)] == [
        ("2030-12-31", 1),
        ("2031-01-01", 1),
    ]


async def test_a_deleted_series_leaves_no_rows_behind_even_before_the_next_refresh(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    keep, _ = await setup(client, auth, name="keep", high=1000)
    gone, _ = await setup(client, auth, name="gone", high=1000)
    await put(engine, keep, (at(1, 10), 1))
    await put(engine, gone, (at(1, 10), 2))
    assert len(await agg(client)) == 2
    assert (await client.delete(f"/api/series/{gone}", headers=auth)).status_code == 204
    assert [r["seriesId"] for r in await agg(client)] == [keep]


async def test_measurements_ingested_through_the_api_are_aggregated(
    client: httpx.AsyncClient, auth: dict[str, str], engine: AsyncEngine
) -> None:
    from tests.integration.test_measurements import send

    series_id, sensor = await setup(client, auth, high=1000)
    for value in (10, 20, 60):
        assert (
            await send(client, sensor["apiKey"], value=value, timestamp="2026-03-01T10:15:00Z")
        ).status_code == 201
    await refresh(engine)  # a past timestamp: the policy would do this within a minute
    (row,) = await agg(client)
    assert (row["seriesId"], row["avg"], row["count"]) == (series_id, 30.0, 3)


# --- parameters ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "field"),
    [
        ("?bucket=1w", "bucket"),
        ("?bucket=", "bucket"),
        ("?bucket=1H", "bucket"),
        ("?series=a,b", "series"),
        ("?series=0", "series"),
        ("?from=yesterday", "from"),
        ("?to=2026-03-01T10:00:00", "to"),  # no UTC offset
        ("?from=2026-03-02T00:00:00Z&to=2026-03-01T00:00:00Z", "from"),
        ("?limit=0", "limit"),
        ("?limit=10001", "limit"),
        ("?limit=x", "limit"),
    ],
)
async def test_bad_parameters_are_a_400_problem(
    client: httpx.AsyncClient, query: str, field: str
) -> None:
    response = await client.get(f"/api/measurements/aggregate{query}")
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["errors"][0]["field"] == field


async def test_it_is_public_and_negotiates_content(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/measurements/aggregate")).status_code == 200  # no credentials
    xml = await client.get("/api/measurements/aggregate", headers={"Accept": "application/xml"})
    assert xml.status_code == 406


async def test_other_methods_are_405(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    for method in ("POST", "PUT", "DELETE"):
        response = await client.request(method, "/api/measurements/aggregate", headers=auth)
        assert response.status_code == 405
