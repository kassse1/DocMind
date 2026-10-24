from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from app.ingestion.chunking import Tokenizer


class EmbeddingError(Exception):
    """The model produced unusable output. The message never contains input text."""


@dataclass(frozen=True)
class ModelSpec:
    dimension: int
    # E5 models are trained with these prefixes and lose quality without them.
    query_prefix: str
    passage_prefix: str


# Only models listed here can be configured: each needs a known dimension (it fixes the
# database column) and its own prefix convention. Adding a model is a deliberate change.
KNOWN_MODELS: dict[str, ModelSpec] = {
    "intfloat/multilingual-e5-small": ModelSpec(384, "query: ", "passage: "),
    "BAAI/bge-m3": ModelSpec(1024, "", ""),
}


class Embedder(Protocol):
    model_name: str
    dimension: int

    @property
    def tokenizer(self) -> Tokenizer: ...

    @property
    def max_input_tokens(self) -> int:
        """Tokens available for passage text after prefix and special tokens."""
        ...

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        """Return a (len(texts), dimension) float32 array of unit-length vectors."""
        ...

    def embed_query(self, text: str) -> np.ndarray:
        """Return one (dimension,) unit-length vector."""
        ...


def validate_embeddings(vectors: object, rows: int, dimension: int) -> np.ndarray:
    """Check shape and values, then L2-normalize so cosine similarity is a dot product."""
    array = np.asarray(vectors, dtype=np.float32)
    if array.shape != (rows, dimension):
        raise EmbeddingError(f"Expected embeddings of shape {(rows, dimension)}, got {array.shape}")
    if not np.isfinite(array).all():
        raise EmbeddingError("Embeddings contain NaN or infinite values")
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    if (norms == 0).any():
        raise EmbeddingError("Embeddings contain a zero vector")
    return array / norms
