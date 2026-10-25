"""Application settings loaded from environment variables (prefix DOCMIND_) and .env."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
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

    # Embeddings. "fake" gives deterministic, meaningless vectors (tests/CI only).
    embedding_backend: Literal["sentence-transformers", "fake"] = "sentence-transformers"
    embedding_model: str = "intfloat/multilingual-e5-small"
    # None = let sentence-transformers choose (CUDA if available, else CPU).
    embedding_device: str | None = None
    embedding_batch_size: int = Field(default=32, ge=1, le=256)
    # None = the Hugging Face default cache (~/.cache/huggingface).
    embedding_cache_dir: Path | None = None
    # false = never download; the model must already be in the cache.
    embedding_allow_download: bool = True

    # Chunking, measured in tokens of the embedding model's tokenizer.
    chunk_size_tokens: int = Field(default=256, ge=16)
    chunk_overlap_tokens: int = Field(default=32, ge=0)
    # Memory bound for one document: all its chunks and vectors are held until the final write.
    max_chunks_per_document: int = Field(default=20_000, ge=1)

    # Semantic search: server-side statement timeout for the vector query.
    search_timeout_ms: int = Field(default=5000, ge=100)

    # Indexing worker (python -m app.worker).
    worker_poll_interval_s: float = Field(default=2.0, gt=0)
    # A running job whose worker has been silent this long is assumed dead and is reclaimed.
    job_lease_timeout_s: int = Field(default=1800, ge=10)
    job_max_attempts: int = Field(default=3, ge=1)
    # Retry n waits n * this many seconds.
    job_retry_delay_s: int = Field(default=30, ge=0)

    @model_validator(mode="after")
    def _check_chunking(self) -> "Settings":
        if self.chunk_overlap_tokens >= self.chunk_size_tokens:
            raise ValueError("chunk_overlap_tokens must be smaller than chunk_size_tokens")
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached settings; tests override via app.dependency_overrides or cache_clear()."""
    return Settings()
