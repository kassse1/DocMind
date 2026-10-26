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

    # Access tokens (app/core/security.py). The secret signs HS256 tokens: at least 32 bytes
    # of random data, kept in .env or the environment, never in code. Required in prod; in
    # dev/test an unset secret is replaced by a random per-process key (tokens then stop
    # working at restart), never by a fixed default.
    jwt_secret_key: SecretStr | None = None
    jwt_issuer: str = "docmind"
    jwt_audience: str = "docmind-api"
    access_token_ttl_minutes: int = Field(default=15, ge=1, le=1440)
    # Refresh tokens rotate on every use; each one expires after refresh_token_ttl_days of
    # disuse, and the session (login) ends after session_max_age_days whatever happens.
    refresh_token_ttl_days: int = Field(default=7, ge=1, le=90)
    session_max_age_days: int = Field(default=30, ge=1, le=365)

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
    # Hybrid search: candidates per branch before fusion (le = schemas.search.MAX_CANDIDATE_POOL)
    # and the RRF constant k in 1 / (k + rank). 60 is the value from the original RRF paper.
    hybrid_candidate_pool: int = Field(default=50, ge=1, le=100)
    hybrid_rrf_k: int = Field(default=60, ge=1, le=1000)

    # Answer generation (POST /api/v1/ask). "disabled" = /ask answers 503
    # generation_unavailable; "fake" = deterministic extractive stand-in (tests/dev, refused
    # in prod); "openai-compatible" = any /v1/chat/completions server (Ollama, llama.cpp,
    # LM Studio, vLLM, hosted APIs).
    generation_backend: Literal["disabled", "fake", "openai-compatible"] = "disabled"
    # Default: a local Ollama server. Configured by the operator, never taken from documents.
    generation_base_url: str = "http://127.0.0.1:11434/v1"
    generation_model: str = "qwen2.5:3b-instruct"
    generation_api_key: SecretStr | None = None
    generation_timeout_s: float = Field(default=120.0, gt=0)
    generation_max_output_tokens: int = Field(default=512, ge=16, le=8192)
    # Ask the server for a JSON object (response_format=json_object); disable for servers
    # that reject the parameter. The output is validated either way.
    generation_json_mode: bool = True
    # Hugging Face tokenizer of the generation model, for exact context token counts
    # (e.g. Qwen/Qwen2.5-3B-Instruct). None = UTF-8 byte count, a conservative upper bound.
    generation_tokenizer: str | None = None
    # Context window of the served model (prompt + reply), e.g. Ollama's num_ctx /
    # OLLAMA_CONTEXT_LENGTH. Ollama does not reject a longer prompt: it silently keeps only
    # its end, dropping the instructions. The passage budget is reduced to fit this window.
    generation_context_window: int = Field(default=4096, ge=512, le=1_000_000)
    # System prompt version (app/rag/prompt.py); reported in every /ask response. v2 is the
    # default after the held-out comparison with qwen2.5:3b-instruct (see README).
    rag_prompt_version: Literal["grounded-qa-v1", "grounded-qa-v2"] = "grounded-qa-v2"
    # "legacy" = Phase 7.1 grounding checks only. "verified" = additionally the
    # model-assisted support check (app/rag/verification.py, two extra model calls); an
    # unsupported or inconclusive verdict abstains. Legacy checks run in both modes.
    rag_grounding_mode: Literal["legacy", "verified"] = "legacy"
    # Model calls per answer when the reply breaks the JSON contract (1 = no retry). A retry
    # appends a short correction and is validated like the first reply.
    generation_max_attempts: int = Field(default=1, ge=1, le=3)
    # Which retrieved chunks reach the prompt (app/rag/selection.py): "rank" = all of them
    # (Phase 7.1), "relative_margin" = only those within rag_similarity_margin of the best.
    rag_context_selection: Literal["rank", "relative_margin"] = "rank"
    rag_similarity_margin: float = Field(default=0.03, ge=0.0, le=2.0)
    # Upper bound on the tokens of all retrieved passages put into the prompt.
    rag_context_token_budget: int = Field(default=3000, ge=64, le=100_000)

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

    @model_validator(mode="after")
    def _check_jwt_secret(self) -> "Settings":
        # Imported here: security imports nothing from config, but keep config import-light.
        from app.core.security import MIN_SECRET_BYTES

        secret = self.jwt_secret_key
        if secret is not None and len(secret.get_secret_value().encode()) < MIN_SECRET_BYTES:
            raise ValueError(f"jwt_secret_key must be at least {MIN_SECRET_BYTES} bytes")
        if secret is None and self.environment == "prod":
            raise ValueError("jwt_secret_key is required when environment is prod")
        return self


@lru_cache
def get_settings() -> Settings:
    """Cached settings; tests override via app.dependency_overrides or cache_clear()."""
    return Settings()
