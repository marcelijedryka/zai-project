import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.main import create_app
from app.security import passwords
from app.services import users
from tests.db_utils import drop_database, migrate, recreate_database

TABLES = "users, sessions, series, sensors, measurements, measurement_rejections"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _cheap_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Argon2 with production parameters costs ~100 ms per call; tests use a cheap profile.

    ``tests/unit/test_passwords.py`` pins the production parameters separately.
    """
    monkeypatch.setattr(passwords, "_hasher", passwords.make_hasher(1, 8 * 1024, 1))
    passwords.dummy_hash.cache_clear()


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


def _scratch_database(base_settings: Settings, suffix: str) -> Iterator[Settings]:
    settings = recreate_database(base_settings, f"{base_settings.postgres_db}_{suffix}")
    yield settings
    drop_database(base_settings, settings.postgres_db)


@pytest.fixture
def migrations_db(base_settings: Settings) -> Iterator[Settings]:
    """An empty, throw-away database for migration tests; dropped afterwards."""
    yield from _scratch_database(base_settings, "migrations")


@pytest.fixture
def models_db(base_settings: Settings) -> Iterator[Settings]:
    """An empty, throw-away database for a schema built straight from the ORM models."""
    yield from _scratch_database(base_settings, "models")


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
async def app(settings: Settings, engine: AsyncEngine) -> AsyncIterator[FastAPI]:
    """The application with its lifespan running; ``engine`` truncates the tables afterwards."""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@dataclass(frozen=True)
class Admin:
    id: int
    username: str
    password: str


@pytest.fixture
async def admin(app: FastAPI) -> Admin:
    async with app.state.session_factory() as session:
        user = await users.create_user(session, ADMIN_USERNAME, ADMIN_PASSWORD)
        return Admin(id=user.id, username=user.username, password=ADMIN_PASSWORD)


@pytest.fixture
async def token(client: httpx.AsyncClient, admin: Admin) -> str:
    """A valid admin Bearer token."""
    response = await client.post(
        "/api/auth/login", json={"username": admin.username, "password": admin.password}
    )
    assert response.status_code == 200
    value: str = response.json()["accessToken"]
    return value


@pytest.fixture
def auth(token: str) -> dict[str, str]:
    """``Authorization`` header for admin requests."""
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def server_options() -> dict[str, Any]:
    """Settings overrides for the ``server`` fixture; a test module can redefine this."""
    return {}


@pytest.fixture
def server_app(settings: Settings, engine: AsyncEngine, server_options: dict[str, Any]) -> FastAPI:
    return create_app(settings.model_copy(update=server_options))


@pytest.fixture
def server(server_app: FastAPI) -> Iterator[str]:
    """The API on 127.0.0.1:<free port>, for clients that need real HTTP (the generator, streams).

    httpx's ASGITransport buffers whole responses, so streaming and synchronous clients need a
    real server; it runs in a background thread and is ready once the realtime listener is
    connected. ``engine`` truncates the tables afterwards. ``server_app.state.uvicorn_server``
    is the uvicorn instance (for shutdown tests).
    """
    config = uvicorn.Config(server_app, host="127.0.0.1", port=0, log_level="warning")
    instance = uvicorn.Server(config)
    server_app.state.uvicorn_server = instance
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    for _ in range(200):
        broadcaster = getattr(server_app.state, "broadcaster", None)
        if instance.started and instance.servers and broadcaster and broadcaster.is_connected:
            break
        threading.Event().wait(0.05)
    else:
        pytest.fail("the test server did not start")
    port = instance.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    instance.should_exit = True
    thread.join(timeout=10)
