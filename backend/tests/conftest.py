from collections.abc import AsyncIterator

import httpx
import pytest

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    """Reads POSTGRES_* from the environment or the repo-root .env (``make dev`` creates it)."""
    return Settings(environment="test")


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c
