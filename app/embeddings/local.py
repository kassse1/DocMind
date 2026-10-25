"""Local sentence-transformers model. No paid API, no network calls after the first download.

The model is loaded lazily on first use and then reused for every batch. On the first
load, the Hugging Face Hub library downloads the weights into its cache
(DOCMIND_EMBEDDING_CACHE_DIR, or ~/.cache/huggingface by default); with
DOCMIND_EMBEDDING_ALLOW_DOWNLOAD=false it only uses files already in the cache.
"""

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from app.embeddings.base import KNOWN_MODELS, validate_embeddings

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


class HuggingFaceTokenizer:
    def __init__(self, tokenizer: Any) -> None:
        self._tokenizer = tokenizer

    def token_offsets(self, text: str) -> list[tuple[int, int]]:
        # verbose=False: texts longer than the model limit are expected here (we only count),
        # and the library would otherwise warn on every one of them.
        encoded = self._tokenizer(
            text, add_special_tokens=False, return_offsets_mapping=True, verbose=False
        )
        return [tuple(span) for span in encoded["offset_mapping"]]


class SentenceTransformerEmbedder:
    def __init__(
        self,
        model_name: str,
        *,
        device: str | None = None,
        batch_size: int = 32,
        cache_dir: Path | None = None,
        allow_download: bool = True,
    ) -> None:
        spec = KNOWN_MODELS[model_name]
        self.model_name = model_name
        self.dimension = spec.dimension
        self._spec = spec
        self._device = device
        self._batch_size = batch_size
        self._cache_dir = cache_dir
        self._allow_download = allow_download
        self._model: SentenceTransformer | None = None
        self._tokenizer: HuggingFaceTokenizer | None = None
        self._max_input_tokens = 0
        self._lock = threading.Lock()

    def load(self) -> None:
        """Load the model once. Safe to call repeatedly and from several threads."""
        with self._lock:
            if self._model is not None:
                return
            # Imported here: torch takes seconds to import and is not needed by the API yet.
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(
                self.model_name,
                device=self._device,
                cache_folder=str(self._cache_dir) if self._cache_dir else None,
                local_files_only=not self._allow_download,
                # Never execute Python code shipped inside a model repository.
                trust_remote_code=False,
            )
            hf_tokenizer = model.tokenizer
            special = len(hf_tokenizer("", add_special_tokens=True)["input_ids"])
            prefix = len(
                hf_tokenizer(self._spec.passage_prefix, add_special_tokens=False)["input_ids"]
            )
            self._max_input_tokens = model.max_seq_length - special - prefix
            self._tokenizer = HuggingFaceTokenizer(hf_tokenizer)
            self._model = model

    def _loaded(self) -> "SentenceTransformer":
        if self._model is None:
            self.load()
        assert self._model is not None
        return self._model

    @property
    def tokenizer(self) -> HuggingFaceTokenizer:
        self._loaded()
        assert self._tokenizer is not None
        return self._tokenizer

    @property
    def max_input_tokens(self) -> int:
        self._loaded()
        return self._max_input_tokens

    @property
    def device(self) -> str:
        return str(self._loaded().device)

    def _encode(self, texts: list[str]) -> np.ndarray:
        vectors = self._loaded().encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return validate_embeddings(vectors, len(texts), self.dimension)

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        return self._encode([self._spec.passage_prefix + text for text in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._encode([self._spec.query_prefix + text])[0]
