"""Opt-in checks against the real embedding model (downloads it on first run).

    $env:DOCMIND_RUN_MODEL_TESTS = "1"; pytest -m model -v

These are sanity checks of the integration (shapes, prefixes, tokenizer limits,
cross-lingual behavior on a few hand-written pairs), not a retrieval-quality benchmark.
Quality is measured on a labeled dataset in Phase 5.
"""

import os

import numpy as np
import pytest

from app.core.config import Settings
from app.embeddings.factory import create_embedder
from app.ingestion.chunking import Chunker

pytestmark = pytest.mark.model

if os.environ.get("DOCMIND_RUN_MODEL_TESTS") != "1":
    pytest.skip("set DOCMIND_RUN_MODEL_TESTS=1 to load the real model", allow_module_level=True)

DIM = 384


@pytest.fixture(scope="module")
def embedder():
    return create_embedder(Settings(_env_file=None, environment="test"), DIM)


def test_vectors_are_normalized_with_expected_dimension(embedder) -> None:
    vectors = embedder.embed_passages(["Hello", "Привет", "Сәлем"])
    assert vectors.shape == (3, DIM)
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, rtol=1e-4)


def test_same_text_gives_same_vector(embedder) -> None:
    a = embedder.embed_passages(["Келісімшарт Алматыда қол қойылды."])
    b = embedder.embed_passages(["Келісімшарт Алматыда қол қойылды."])
    np.testing.assert_allclose(a, b, atol=1e-5)


@pytest.mark.parametrize(
    ("query", "relevant", "unrelated"),
    [
        (
            "When was the contract signed?",
            "Договор был подписан пятого марта в Алматы.",
            "Рецепт борща: свёкла, капуста и картофель.",
        ),
        (
            "When was the contract signed?",
            "Келісімшарт бесінші наурызда Алматыда қол қойылды.",
            "Бүгін ауа райы жылы әрі шуақты.",
        ),
        (
            "Когда был подписан договор?",
            "The agreement was signed on March 5 in Almaty.",
            "The recipe needs flour, eggs and sugar.",
        ),
    ],
)
def test_cross_lingual_query_prefers_relevant_passage(embedder, query, relevant, unrelated) -> None:
    q = embedder.embed_query(query)
    passages = embedder.embed_passages([relevant, unrelated])
    scores = passages @ q
    assert scores[0] > scores[1]


def test_chunks_fit_model_input_limit(embedder) -> None:
    text = "\n\n".join(
        ["Қазақстан Республикасының заңнамасы бойынша келісімшарт жасалды. " * 30] * 5
    )
    chunker = Chunker(embedder.tokenizer, chunk_size=256, overlap=32)
    chunks = list(chunker.chunk_pages([(None, text)]))
    assert len(chunks) > 1
    assert all(c.token_count <= embedder.max_input_tokens for c in chunks)
    assert embedder.max_input_tokens < 512
