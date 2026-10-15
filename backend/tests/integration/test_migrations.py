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
from tests.db_utils import downgrade, migrate, upgrade_sql

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


# --- migration 0004: the hourly continuous aggregate ---------------------------------------------


async def _query(settings: Settings, sql: str) -> list[tuple[Any, ...]]:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        rows = (await conn.execute(text(sql))).all()
    await engine.dispose()
    return [tuple(r) for r in rows]


CAGG = (
    "SELECT hypertable_name, materialized_only "
    "FROM timescaledb_information.continuous_aggregates WHERE view_name = 'measurements_hourly'"
)
POLICY = (
    "SELECT schedule_interval::text, config->>'start_offset', config->>'end_offset' "
    "FROM timescaledb_information.jobs WHERE proc_name = 'policy_refresh_continuous_aggregate'"
)


async def test_the_hourly_aggregate_is_real_time_with_a_refresh_policy(
    test_settings: Settings,
) -> None:
    assert await _query(test_settings, CAGG) == [("measurements", False)]
    # Every minute, over the whole history (backfilled data) up to one hour ago.
    assert await _query(test_settings, POLICY) == [("00:01:00", None, "01:00:00")]
    columns = await _query(
        test_settings,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'measurements_hourly' ORDER BY ordinal_position",
    )
    assert [c[0] for c in columns] == [
        "series_id", "bucket", "sample_count", "value_sum", "value_min", "value_max",
    ]  # fmt: skip


async def test_downgrading_0004_removes_the_aggregate_its_policy_and_storage(
    migrations_db: Settings,
) -> None:
    migrate(migrations_db)
    assert await _query(migrations_db, CAGG)
    downgrade(migrations_db, "0003")
    assert await _query(migrations_db, CAGG) == []
    assert await _query(migrations_db, POLICY) == []
    assert (
        await _query(migrations_db, "SELECT 1 FROM pg_class WHERE relname = 'measurements_hourly'")
        == []
    )
    internal = await _query(
        migrations_db,
        "SELECT 1 FROM pg_tables WHERE tablename LIKE '_materialized_hypertable_%'",
    )
    assert internal == []  # the hidden storage hypertable went with it
    migrate(migrations_db)
    assert await _query(migrations_db, CAGG) == [("measurements", False)]


async def test_the_offline_script_steps_out_of_the_transaction_for_the_aggregate(
    migrations_db: Settings,
) -> None:
    """``make schema-sql`` (``alembic upgrade head --sql``) must still produce a valid script."""
    script = upgrade_sql(migrations_db)
    create = script.index("CREATE MATERIALIZED VIEW IF NOT EXISTS measurements_hourly")
    assert script.rindex("COMMIT;", 0, create) > script.index("-- Running upgrade 0003 -> 0004")
    policy = script.index("add_continuous_aggregate_policy")
    assert "BEGIN;" in script[policy:]  # the transaction resumes for the version bookkeeping
    assert "timescaledb.materialized_only = false" in script


def test_the_aggregate_and_its_hidden_storage_are_not_model_objects() -> None:
    class Hidden:
        schema = "_timescaledb_internal"

    assert not include_object(None, "measurements_hourly", "table")
    assert not include_object(Hidden(), "_materialized_hypertable_2", "table")
    assert include_object(None, "measurements", "table")
