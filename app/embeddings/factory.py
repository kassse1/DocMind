from app.core.config import Settings
from app.embeddings.base import KNOWN_MODELS, Embedder


def create_embedder(settings: Settings, dimension: int) -> Embedder:
    """Build the configured embedder; `dimension` is what the database column stores."""
    if settings.embedding_backend == "fake":
        if settings.environment == "prod":
            raise ValueError("The fake embedding backend is not allowed in prod")
        from app.embeddings.fake import FakeEmbedder

        return FakeEmbedder(dimension)

    spec = KNOWN_MODELS.get(settings.embedding_model)
    if spec is None:
        raise ValueError(
            f"Unknown embedding model {settings.embedding_model!r}; "
            f"supported: {sorted(KNOWN_MODELS)}"
        )
    if spec.dimension != dimension:
        # The vector column has a fixed size: a different model needs a migration + re-index.
        raise ValueError(
            f"{settings.embedding_model} produces {spec.dimension}-dimensional vectors, "
            f"but the database stores {dimension}. Changing models requires a migration."
        )
    from app.embeddings.local import SentenceTransformerEmbedder

    return SentenceTransformerEmbedder(
        settings.embedding_model,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
        cache_dir=settings.embedding_cache_dir,
        allow_download=settings.embedding_allow_download,
    )
