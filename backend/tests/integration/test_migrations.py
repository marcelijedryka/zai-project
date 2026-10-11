"""Migrations apply, roll back and match the ORM models."""

import asyncio

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.models import Base
from tests.db_utils import downgrade, migrate, recreate_database

# Created by TimescaleDB itself, see alembic/env.py.
TIMESCALE_INDEXES = {"measurements_ts_idx"}


def _include(obj: object, name: str | None, type_: str, *_: object) -> bool:
    return not (type_ == "index" and name in TIMESCALE_INDEXES)


async def _tables(settings: Settings) -> set[str]:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        names = {r[0] for r in rows}
    await engine.dispose()
    return names


async def test_upgrade_downgrade_upgrade(base_settings: Settings) -> None:
    settings = await asyncio.to_thread(
        recreate_database, base_settings, f"{base_settings.postgres_db}_migrations"
    )
    expected = {
        "users",
        "sessions",
        "series",
        "sensors",
        "measurements",
        "measurement_rejections",
    }
    await asyncio.to_thread(migrate, settings)
    assert expected <= await _tables(settings)
    await asyncio.to_thread(downgrade, settings)
    assert not expected & await _tables(settings)
    await asyncio.to_thread(migrate, settings)
    assert expected <= await _tables(settings)


async def test_models_match_migrations(test_settings: Settings) -> None:
    engine = create_async_engine(test_settings.database_url, poolclass=NullPool)

    def diff(sync_conn: object) -> list[object]:
        ctx = MigrationContext.configure(sync_conn, opts={"include_object": _include})  # type: ignore[arg-type]
        return list(compare_metadata(ctx, Base.metadata))

    async with engine.connect() as conn:
        differences = await conn.run_sync(diff)
    await engine.dispose()
    assert differences == []
