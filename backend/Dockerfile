# syntax=docker/dockerfile:1
FROM python:3.13.15-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app
RUN useradd --system --uid 10001 --create-home app

FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /bin/uv
COPY pyproject.toml uv.lock .python-version ./

FROM builder AS deps-prod
RUN uv sync --frozen --no-dev --no-install-project

FROM builder AS deps-dev
RUN uv sync --frozen --no-install-project

# Local development: dev dependencies, source is bind-mounted, auto-reload.
FROM base AS dev
# The bind-mounted source may not be writable by uid 10001 (Linux hosts): keep tool caches in /tmp.
ENV RUFF_CACHE_DIR=/tmp/ruff_cache \
    MYPY_CACHE_DIR=/tmp/mypy_cache \
    PYTEST_ADDOPTS="-o cache_dir=/tmp/pytest_cache"
COPY --from=deps-dev /opt/venv /opt/venv
USER app
CMD ["uvicorn", "app.main:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--reload", "--no-access-log"]

# Behind the reverse proxy set FORWARDED_ALLOW_IPS to the proxy network only (never "*" while
# the port is published): it decides whose X-Forwarded-For is trusted, hence the client IP used
# for login throttling and the sessions table.
# Production: runtime dependencies only, source baked in, non-root.
FROM base AS runtime
COPY --from=deps-prod /opt/venv /opt/venv
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-server-header", "--no-access-log"]
