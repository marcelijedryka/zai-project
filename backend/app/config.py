"""Application settings, read from the environment (see ``.env.example``)."""

import re
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import URL

# Repo-root .env for host-side runs (uvicorn, pytest, alembic); in containers the
# variables come from compose and this path simply does not exist.
# A browser Origin is exactly scheme://host[:port]; "*" with credentials would reflect any origin.
_ORIGIN = re.compile(r"https?://[^/\s?#*]+")

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    postgres_user: str
    postgres_password: SecretStr
    postgres_db: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    environment: Literal["dev", "test", "prod"] = "dev"
    # Comma-separated in the environment; explicit allow-list, never "*" (credentials are on).
    # These origins are also the only ones trusted for cookie-authenticated unsafe requests.
    cors_allowed_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]
    # The contract allows past timestamps; this is the one deliberate limit on that. Older
    # measurements are refused with 422. It is a floor (not a rolling window) because the official
    # tests use fixed past dates, and it bounds the 7-day Timescale chunks (about 52 a year) that
    # a query over all data must lock: measured, 1,370 scattered chunks still work (an unfiltered
    # listing takes ~0.5 s), 2,900 broke `count(*)` with "out of shared memory".
    earliest_measurement_timestamp: AwareDatetime = datetime(2000, 1, 1, tzinfo=UTC)
    # Rejected measurements are kept for the admin error-log view, then purged hourly. A faulty
    # sensor can only add this many rows per minute (the rest is still refused and logged).
    rejection_retention_days: int = Field(default=30, ge=1)
    rejection_rows_per_sensor_per_minute: int = Field(default=60, ge=1)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    error_log_file: Path | None = None

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("cors_allowed_origins")
    @classmethod
    def _check_origins(cls, origins: list[str]) -> list[str]:
        for origin in origins:
            if not _ORIGIN.fullmatch(origin):
                raise ValueError(
                    f"invalid CORS origin {origin!r}: use scheme://host[:port] "
                    "(no wildcard, path or trailing slash)"
                )
        return origins

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @property
    def database_url(self) -> URL:
        """Built from parts, so passwords with ``@ / : # %`` need no manual escaping."""
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @property
    def docs_enabled(self) -> bool:
        return self.environment != "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
