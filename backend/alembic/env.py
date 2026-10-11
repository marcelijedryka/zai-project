"""Alembic environment (async). The URL comes from app settings, or from the
``sqlalchemy.url`` option when a caller (e.g. the tests) sets it explicitly."""

import asyncio

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.config import get_settings
from app.models import Base

config = context.config
target_metadata = Base.metadata

# Objects created by TimescaleDB itself, not described by our models.
TIMESCALE_INDEXES = {"measurements_ts_idx"}


def include_object(obj: object, name: str | None, type_: str, *_: object) -> bool:
    return not (type_ == "index" and name in TIMESCALE_INDEXES)


def run_migrations_offline() -> None:
    """Emit SQL without connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url="postgresql+asyncpg://",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, include_object=include_object
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url") or get_settings().database_url
    engine = create_async_engine(url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
