"""FastAPI application factory."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from app.api import auth, health, measurements, sensors, series
from app.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.middleware import (
    CatchAllErrorsMiddleware,
    NegotiationMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.core.paths import DOCS_PATH
from app.core.problems import (
    ProblemError,
    http_exception_handler,
    problem_error_handler,
    validation_exception_handler,
)
from app.db import create_engine, create_session_factory
from app.security.passwords import HashingBusy, warm_up
from app.security.rate_limit import LoginThrottle
from app.tasks import housekeeping_forever


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        app.state.login_throttle = LoginThrottle()  # per (client, username)
        app.state.ip_throttle = LoginThrottle(max_failures=30)  # per client, any username
        await warm_up()
        app.state.rejection_limiter = LoginThrottle(
            max_failures=settings.rejection_rows_per_sensor_per_minute, window_seconds=60
        )
        purge_task = asyncio.create_task(
            housekeeping_forever(app.state.session_factory, settings.rejection_retention_days)
        )
        try:
            yield
        finally:
            purge_task.cancel()
            with suppress(asyncio.CancelledError):
                await purge_task
            await engine.dispose()

    app = FastAPI(
        title="ZAI 26Z - Warsaw Environment Monitor",
        version="0.1.0",
        lifespan=lifespan,
        redirect_slashes=False,
        docs_url=DOCS_PATH if settings.docs_enabled else None,
        redoc_url=None,
        # Schema stays public in prod on purpose (plan §5.5); only Swagger UI is dev-only.
        openapi_url="/api/openapi.json",
    )

    app.add_exception_handler(ProblemError, problem_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(HashingBusy, auth.hashing_busy_handler)

    # Added innermost-first: the last one added is the outermost layer.
    app.add_middleware(NegotiationMiddleware)
    app.add_middleware(CatchAllErrorsMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Accept", "Authorization", "Content-Type", "X-API-Key"],
        expose_headers=["Location", "X-Total-Count", "X-Request-ID"],
        max_age=600,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    app.state.settings = settings
    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(series.router, prefix="/api")
    app.include_router(sensors.router, prefix="/api")
    app.include_router(measurements.router, prefix="/api")
    return app


def get_app() -> FastAPI:
    """Uvicorn factory entry point: ``uvicorn app.main:get_app --factory``."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.error_log_file)
    return create_app(settings)
