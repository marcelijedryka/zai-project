"""Application settings, read from the environment (see ``.env.example``)."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    environment: Literal["dev", "test", "prod"] = "dev"

    @property
    def docs_enabled(self) -> bool:
        return self.environment != "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
