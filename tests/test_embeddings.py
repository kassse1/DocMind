import math

import numpy as np
import pytest

from app.core.config import Settings
from app.embeddings.base import EmbeddingError, validate_embeddings
from app.embeddings.factory import create_embedder
from app.embeddings.fake import FakeEmbedder

DIM = 384


def test_fake_embeddings_have_dimension_and_unit_length() -> None:
    vectors = FakeEmbedder(DIM).embed_passages(["one", "два", "үш"])
    assert vectors.shape == (3, DIM)
    assert vectors.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, rtol=1e-5)


def test_fake_embeddings_are_deterministic_across_instances() -> None:
    a = FakeEmbedder(DIM).embed_passages(["Келісімшарт", "contract"])
    b = FakeEmbedder(DIM).embed_passages(["Келісімшарт", "contract"])
    np.testing.assert_array_equal(a, b)
    assert not np.allclose(a[0], a[1])


def test_fake_query_and_passage_vectors_differ_like_prefixed_models() -> None:
    embedder = FakeEmbedder(DIM)
    assert embedder.embed_query("text").shape == (DIM,)
    assert not np.allclose(embedder.embed_query("text"), embedder.embed_passages(["text"])[0])


def test_fake_embed_empty_batch() -> None:
    assert FakeEmbedder(DIM).embed_passages([]).shape == (0, DIM)


def test_validate_normalizes() -> None:
    result = validate_embeddings([[3.0, 4.0]], 1, 2)
    np.testing.assert_allclose(result, [[0.6, 0.8]], rtol=1e-6)


@pytest.mark.parametrize(
    ("vectors", "rows", "dim"),
    [
        ([[1.0, 0.0]], 1, 3),            # wrong dimension
        ([[1.0, 0.0]], 2, 2),            # wrong count
        ([[math.nan, 1.0]], 1, 2),
        ([[math.inf, 1.0]], 1, 2),
        ([[0.0, 0.0]], 1, 2),            # cannot be normalized
    ],
)
def test_validate_rejects_bad_vectors(vectors: list[list[float]], rows: int, dim: int) -> None:
    with pytest.raises(EmbeddingError):
        validate_embeddings(vectors, rows, dim)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, environment="test", **overrides)


def test_factory_builds_fake_backend() -> None:
    embedder = create_embedder(_settings(embedding_backend="fake"), DIM)
    assert embedder.dimension == DIM


def test_factory_refuses_fake_backend_in_prod() -> None:
    settings = Settings(
        _env_file=None, environment="prod", embedding_backend="fake", jwt_secret_key="x" * 32
    )
    with pytest.raises(ValueError, match="not allowed in prod"):
        create_embedder(settings, DIM)


def test_factory_rejects_unknown_model() -> None:
    with pytest.raises(ValueError, match="Unknown embedding model"):
        create_embedder(_settings(embedding_model="someone/random-model"), DIM)


def test_factory_rejects_dimension_mismatch_with_database() -> None:
    with pytest.raises(ValueError, match="requires a migration"):
        create_embedder(_settings(embedding_model="BAAI/bge-m3"), DIM)


def test_factory_does_not_load_the_real_model_eagerly() -> None:
    embedder = create_embedder(_settings(), DIM)
    assert embedder.model_name == "intfloat/multilingual-e5-small"
    assert embedder._model is None  # loading (and downloading) happens on first use


def test_settings_reject_overlap_not_smaller_than_chunk_size() -> None:
    with pytest.raises(ValueError, match="chunk_overlap_tokens"):
        _settings(chunk_size_tokens=64, chunk_overlap_tokens=64)
