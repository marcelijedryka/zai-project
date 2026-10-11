"""Migrations apply, roll back and match the ORM models."""

from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.models import TIMESCALE_MANAGED_INDEXES, Base, include_object
from tests.db_utils import downgrade, migrate

EXPECTED_TABLES = {
    "users",
    "sessions",
    "series",
    "sensors",
    "measurements",
    "measurement_rejections",
}


async def _tables(conn: AsyncConnection) -> set[str]:
    rows = await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    return {r[0] for r in rows}


async def test_upgrade_downgrade_upgrade(migrations_db: Settings) -> None:
    engine = create_async_engine(migrations_db.database_url, poolclass=NullPool)

    async def tables() -> set[str]:
        async with engine.connect() as conn:
            return await _tables(conn)

    migrate(migrations_db)
    assert await tables() >= EXPECTED_TABLES
    downgrade(migrations_db)
    assert not EXPECTED_TABLES & await tables()
    migrate(migrations_db)
    assert await tables() >= EXPECTED_TABLES
    await engine.dispose()


async def test_models_match_migrations(test_settings: Settings) -> None:
    """Tables, columns, types, nullability, server defaults, indexes and FKs.

    Alembic cannot see CHECK constraints or partial-index predicates; the next test covers them.
    """
    engine = create_async_engine(test_settings.database_url, poolclass=NullPool)

    def diff(sync_conn: Connection) -> list[Any]:
        ctx = MigrationContext.configure(
            sync_conn, opts={"include_object": include_object, "compare_server_default": True}
        )
        return list(compare_metadata(ctx, Base.metadata))

    async with engine.connect() as conn:
        differences = await conn.run_sync(diff)
    await engine.dispose()
    assert differences == []


async def _definitions(settings: Settings) -> dict[str, str]:
    """Constraint and index definitions as PostgreSQL itself normalises them."""
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        constraints = await conn.execute(
            text(
                "SELECT 'constraint ' || conrelid::regclass || '.' || conname, "
                "pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE connamespace = 'public'::regnamespace "
                "AND conrelid::regclass::text <> 'alembic_version'"
            )
        )
        indexes = await conn.execute(
            text(
                "SELECT 'index ' || indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version' "
                "AND indexname <> ALL(:managed)"
            ),
            {"managed": sorted(TIMESCALE_MANAGED_INDEXES)},
        )
        result = dict([*constraints, *indexes])
    await engine.dispose()
    return result


async def test_constraints_and_indexes_match_models(
    test_settings: Settings, models_db: Settings
) -> None:
    """CHECK constraints, partial-index predicates and expression indexes (alembic blind spots).

    Builds a second database with ``create_all`` from the models and compares
    ``pg_get_constraintdef`` / ``pg_get_indexdef`` with the migrated one.
    """
    engine = create_async_engine(models_db.database_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    from_models = await _definitions(models_db)
    from_migrations = await _definitions(test_settings)
    assert from_migrations == from_models
