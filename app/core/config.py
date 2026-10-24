"""Application settings loaded from environment variables (prefix DOCMIND_) and .env."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
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

    # SecretStr keeps the password out of repr(), logs and tracebacks.
    # Placeholder for local development only; set DOCMIND_DATABASE_URL in .env or the environment.
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://docmind:docmind@127.0.0.1:5432/docmind"
    )
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)
    db_pool_timeout_s: float = Field(default=5.0, gt=0)
    db_connect_timeout_s: int = Field(default=3, ge=1)
    # Upper bound for the readiness probe query, so a stuck database cannot hang the probe.
    db_readiness_timeout_ms: int = Field(default=2000, ge=100)

    # Uploaded originals live here, outside the repository, under server-generated names.
    storage_dir: Path = Path.home() / ".docmind" / "storage"
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    max_pdf_pages: int = Field(default=2000, ge=1)
    # Zip bomb guard: total uncompressed size of all members of a DOCX archive.
    max_docx_uncompressed_bytes: int = Field(default=50 * 1024 * 1024, ge=1)

    # Development-only identity until authentication exists (Phase 8). Ignored in prod,
    # where document endpoints refuse requests instead of falling back to a shared user.
    dev_user_email: str = "dev@docmind.local"


@lru_cache
def get_settings() -> Settings:
    """Cached settings; tests override via app.dependency_overrides or cache_clear()."""
    return Settings()
