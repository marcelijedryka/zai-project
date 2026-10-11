"""Database constraints, indexes, cascades and the Timescale hypertable (plan §4)."""

import math
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

TS = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64


async def _series(conn: AsyncConnection, **over: Any) -> int:
    params: dict[str, Any] = {
        "name": "Temp",
        "min": 0.0,
        "max": 100.0,
        "color": "#1f77b4",
    } | over
    result = await conn.execute(
        text(
            "INSERT INTO series (name, min_value, max_value, color) "
            "VALUES (:name, :min, :max, :color) RETURNING id"
        ),
        params,
    )
    return int(result.scalar_one())


async def _sensor(conn: AsyncConnection, series_id: int, key_hash: str = HASH_A) -> int:
    result = await conn.execute(
        text("INSERT INTO sensors (series_id, name, key_hash) VALUES (:s, 's', :h) RETURNING id"),
        {"s": series_id, "h": key_hash},
    )
    return int(result.scalar_one())


async def _measurement(
    conn: AsyncConnection, series_id: int, sensor_id: int | None, value: float = 1.0
) -> int:
    result = await conn.execute(
        text(
            "INSERT INTO measurements (series_id, sensor_id, value, ts) "
            "VALUES (:s, :sn, :v, :ts) RETURNING id"
        ),
        {"s": series_id, "sn": sensor_id, "v": value, "ts": TS},
    )
    return int(result.scalar_one())


async def _count(conn: AsyncConnection, table: str) -> int:
    assert table in {"sensors", "measurements", "measurement_rejections", "series"}
    return int((await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608


async def test_measurements_is_a_hypertable_with_7_day_chunks(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT column_name, time_interval::text "
                    "FROM timescaledb_information.dimensions "
                    "WHERE hypertable_name = 'measurements'"
                )
            )
        ).all()
    assert [tuple(r) for r in rows] == [("ts", "7 days")]


async def test_expected_indexes_exist(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        names = {
            r[0]
            for r in await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            )
        }
    assert {
        "ix_measurements_series_id_ts",
        "ix_measurements_id",
        "uq_series_name_lower",
        "uq_sensors_key_hash_active",
        "ix_sensors_series_id",
        "ix_sessions_expires_at",
        "ix_measurement_rejections_created_at",
    } <= names


@pytest.mark.parametrize(
    "override",
    [
        {"min": 5.0, "max": 5.0},
        {"min": 10.0, "max": 1.0},
        {"max": math.nan},
        {"min": -math.inf},
        {"max": math.inf},
        {"color": "red"},
        {"color": "#12345"},
        {"color": "#12345g"},
        {"name": ""},
    ],
)
async def test_series_check_constraints(engine: AsyncEngine, override: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await _series(conn, **override)


async def test_series_name_is_unique_case_insensitively(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await _series(conn, name="Warsaw PM2.5")
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await _series(conn, name="warsaw pm2.5")


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
async def test_measurement_value_must_be_finite(engine: AsyncEngine, value: float) -> None:
    async with engine.begin() as conn:
        series_id = await _series(conn)
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await _measurement(conn, series_id, None, value)


async def test_measurement_value_round_trips_exactly(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        series_id = await _series(conn)
        mid = await _measurement(conn, series_id, None, 21.5)
        stored: float = (
            await conn.execute(text("SELECT value FROM measurements WHERE id = :i"), {"i": mid})
        ).scalar_one()
    assert stored == 21.5


async def test_two_measurements_may_share_a_timestamp(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        series_id = await _series(conn)
        first = await _measurement(conn, series_id, None)
        second = await _measurement(conn, series_id, None)
    assert first != second


async def test_active_sensor_key_hashes_are_unique_but_revoked_ones_are_free(
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as conn:
        series_id = await _series(conn)
        first = await _sensor(conn, series_id, HASH_A)
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await _sensor(conn, series_id, HASH_A)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE sensors SET revoked_at = now() WHERE id = :i"), {"i": first}
        )
        await _sensor(conn, series_id, HASH_A)


async def test_sensor_key_hash_must_be_64_lowercase_hex(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        series_id = await _series(conn)
    for bad in ("short", "G" * 64, "A" * 64):
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await _sensor(conn, series_id, bad)


async def test_deleting_a_series_cascades(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        series_id = await _series(conn)
        sensor_id = await _sensor(conn, series_id)
        await _measurement(conn, series_id, sensor_id)
        await conn.execute(
            text(
                "INSERT INTO measurement_rejections (series_id, sensor_id, value, ts, reason) "
                "VALUES (:s, :sn, 999, :ts, 'out_of_range')"
            ),
            {"s": series_id, "sn": sensor_id, "ts": TS},
        )
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM series WHERE id = :i"), {"i": series_id})
        counts = [
            await _count(conn, t) for t in ("sensors", "measurements", "measurement_rejections")
        ]
    assert counts == [0, 0, 0]


async def test_deleting_a_sensor_keeps_its_measurements(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        series_id = await _series(conn)
        sensor_id = await _sensor(conn, series_id)
        mid = await _measurement(conn, series_id, sensor_id)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM sensors WHERE id = :i"), {"i": sensor_id})
        row = (
            await conn.execute(text("SELECT sensor_id FROM measurements WHERE id = :i"), {"i": mid})
        ).one()
    assert row[0] is None


async def test_rejection_reason_is_restricted(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        series_id = await _series(conn)
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO measurement_rejections (series_id, value, ts, reason) "
                    "VALUES (:s, 1, :ts, 'because')"
                ),
                {"s": series_id, "ts": TS},
            )
