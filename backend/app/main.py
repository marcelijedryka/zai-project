"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health
from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="ZAI 26Z - Warsaw Environment Monitor",
        version="0.1.0",
        lifespan=lifespan,
        redirect_slashes=False,
        docs_url="/api/docs" if settings.docs_enabled else None,
        redoc_url=None,
        # Schema stays public in prod on purpose (plan §5.5); only Swagger UI is dev-only.
        openapi_url="/api/openapi.json",
    )
    app.include_router(health.router, prefix="/api")
    return app


def get_app() -> FastAPI:
    """Uvicorn factory entry point: ``uvicorn app.main:get_app --factory``."""
    return create_app()
