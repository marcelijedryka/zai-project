"""Helpers to create throw-away databases and run Alembic against them.

Alembic's async ``env.py`` and the helpers below call ``asyncio.run``; every public helper
therefore runs in a fresh worker thread, so it is safe whether or not the caller has an
event loop running (sync fixtures, async tests, any pytest-asyncio loop scope).
"""

import asyncio
import io
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from pathlib import Path

import asyncpg
from alembic.config import Config

from alembic import command
from app.config import Settings

BACKEND_DIR = Path(__file__).resolve().parents[1]


def off_event_loop[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(fn, *args, **kwargs).result()

    return wrapper


def alembic_config(settings: Settings) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    url = settings.database_url.render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))  # configparser escaping
    return config


@off_event_loop
def migrate(settings: Settings, target: str = "head") -> None:
    command.upgrade(alembic_config(settings), target)


@off_event_loop
def downgrade(settings: Settings, target: str = "base") -> None:
    command.downgrade(alembic_config(settings), target)


@off_event_loop
def upgrade_sql(settings: Settings) -> str:
    """The offline script (``alembic upgrade head --sql``), as ``make schema-sql`` writes it."""
    config = alembic_config(settings)
    config.output_buffer = io.StringIO()
    command.upgrade(config, "head", sql=True)
    return config.output_buffer.getvalue()


async def _admin_execute(settings: Settings, *statements: str) -> None:
    conn = await asyncpg.connect(
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        host=settings.postgres_host,
        port=settings.postgres_port,
        database="postgres",
    )
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@off_event_loop
def recreate_database(settings: Settings, name: str) -> Settings:
    """Drop and create database ``name``; return settings pointing at it."""
    asyncio.run(
        _admin_execute(
            settings,
            f"DROP DATABASE IF EXISTS {_quote(name)} WITH (FORCE)",
            f"CREATE DATABASE {_quote(name)}",
        )
    )
    return settings.model_copy(update={"postgres_db": name})


@off_event_loop
def drop_database(settings: Settings, name: str) -> None:
    asyncio.run(_admin_execute(settings, f"DROP DATABASE IF EXISTS {_quote(name)} WITH (FORCE)"))
