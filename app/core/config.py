"""Application settings loaded from environment variables (prefix DOCMIND_) and .env."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DOCMIND_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "DocMind"
    app_version: str = "0.1.0"
    environment: Literal["dev", "test", "prod"] = "dev"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Cached settings; tests override via app.dependency_overrides or cache_clear()."""
    return Settings()
