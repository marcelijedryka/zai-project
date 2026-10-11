"""Application settings, read from the environment (see ``.env.example``)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

# Repo-root .env for host-side runs (uvicorn, pytest, alembic); in containers the
# variables come from compose and this path simply does not exist.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    postgres_user: str
    postgres_password: SecretStr
    postgres_db: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    environment: Literal["dev", "test", "prod"] = "dev"

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
