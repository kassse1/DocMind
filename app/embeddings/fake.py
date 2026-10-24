"""Deterministic stand-in for a real model: for tests, CI and development without downloads.

Vectors are derived from a SHA-256 of the text, so equal texts always map to the same
vector, but similarity carries no meaning. Never use it to judge retrieval quality.
"""

import hashlib
import re
from collections.abc import Sequence

import numpy as np

from app.embeddings.base import validate_embeddings

_WORD = re.compile(r"\S+")


class WordTokenizer:
    def token_offsets(self, text: str) -> list[tuple[int, int]]:
        return [match.span() for match in _WORD.finditer(text)]


class FakeEmbedder:
    model_name = "fake/sha256"

    def __init__(self, dimension: int, max_input_tokens: int = 512) -> None:
        self.dimension = dimension
        self._max_input_tokens = max_input_tokens
        self._tokenizer = WordTokenizer()

    @property
    def tokenizer(self) -> WordTokenizer:
        return self._tokenizer

    @property
    def max_input_tokens(self) -> int:
        return self._max_input_tokens

    def _vector(self, text: str) -> np.ndarray:
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        return np.random.default_rng(seed).standard_normal(self.dimension)

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        vectors = [self._vector("passage: " + text) for text in texts]
        return validate_embeddings(
            np.array(vectors).reshape(len(texts), self.dimension), len(texts), self.dimension
        )

    def embed_query(self, text: str) -> np.ndarray:
        return validate_embeddings([self._vector("query: " + text)], 1, self.dimension)[0]
