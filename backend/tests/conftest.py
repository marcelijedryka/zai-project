from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.main import create_app
from tests.db_utils import migrate, recreate_database

TABLES = "users, sessions, series, sensors, measurements, measurement_rejections"


@pytest.fixture(scope="session")
def base_settings() -> Settings:
    """Reads POSTGRES_* from the environment or the repo-root .env (``make dev`` creates it)."""
    return Settings(environment="test")


@pytest.fixture(scope="session")
def test_settings(base_settings: Settings) -> Settings:
    """Settings for a freshly migrated ``<db>_test`` database, created once per session."""
    settings = recreate_database(base_settings, f"{base_settings.postgres_db}_test")
    migrate(settings)
    return settings


@pytest.fixture
def settings(test_settings: Settings) -> Settings:
    return test_settings


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    yield engine
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    await engine.dispose()


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c
