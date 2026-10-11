"""Helpers to create throw-away databases and run Alembic against them."""

import asyncio
from pathlib import Path

import asyncpg
from alembic.config import Config

from alembic import command
from app.config import Settings

BACKEND_DIR = Path(__file__).resolve().parents[1]


def alembic_config(settings: Settings) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    url = settings.database_url.render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))  # configparser escaping
    return config


def migrate(settings: Settings, target: str = "head") -> None:
    command.upgrade(alembic_config(settings), target)


def downgrade(settings: Settings, target: str = "base") -> None:
    command.downgrade(alembic_config(settings), target)


async def _recreate(settings: Settings, name: str) -> None:
    conn = await asyncpg.connect(
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        host=settings.postgres_host,
        port=settings.postgres_port,
        database="postgres",
    )
    try:
        quoted = '"' + name.replace('"', '""') + '"'
        await conn.execute(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {quoted}")
    finally:
        await conn.close()


def recreate_database(settings: Settings, name: str) -> Settings:
    """Drop and create database ``name``; return settings pointing at it."""
    asyncio.run(_recreate(settings, name))
    return settings.model_copy(update={"postgres_db": name})
