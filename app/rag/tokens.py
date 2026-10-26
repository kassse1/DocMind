"""Token counting for the context budget.

Exact counts need the generation model's own tokenizer, which a remote or Ollama-served
model does not expose. Fallback: the UTF-8 byte length. For byte-level BPE tokenizers
(GPT-2 family, Llama 3, Qwen 2.x) every token covers at least one byte, so tokens <= bytes.
SentencePiece tokenizers with byte fallback are within it up to a few boundary tokens per
text (the word marker usually stands in for a space byte); this is not proven for every
tokenizer, so configure the real one when it is known. The bound is
loose: about 3-4 bytes per token for English and Cyrillic text, so the byte counter admits
fewer passages than the budget would really allow. That trade is deliberate: an
underestimate would let the server silently truncate the prompt, cutting off evidence.
"""

import logging
import threading
from typing import Any, Protocol

from app.core.config import Settings
from app.rag.generation import GenerationUnavailableError

logger = logging.getLogger(__name__)


class TokenCounter(Protocol):
    name: str
    # True when counts equal the model's own (not just an upper bound).
    exact: bool

    def count(self, text: str) -> int: ...


class Utf8ByteCounter:
    name = "utf8-bytes-upper-bound"
    exact = False

    def count(self, text: str) -> int:
        return len(text.encode("utf-8"))


class HuggingFaceTokenCounter:
    """Exact counts with the generation model's tokenizer. Loaded lazily on first use, so
    startup does not wait for (or need) the tokenizer files."""

    exact = True

    def __init__(self, tokenizer_name: str) -> None:
        self.name = f"hf:{tokenizer_name}"
        self._tokenizer_name = tokenizer_name
        self._tokenizer: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._tokenizer is None:
                try:
                    from transformers import AutoTokenizer

                    self._tokenizer = AutoTokenizer.from_pretrained(self._tokenizer_name)
                except Exception as exc:
                    logger.error("Generation tokenizer unavailable: %s", type(exc).__name__)
                    raise GenerationUnavailableError("tokenizer unavailable") from None
        return self._tokenizer

    def count(self, text: str) -> int:
        return len(self._load().encode(text, add_special_tokens=False))


def create_token_counter(settings: Settings) -> TokenCounter:
    if settings.generation_tokenizer:
        return HuggingFaceTokenCounter(settings.generation_tokenizer)
    return Utf8ByteCounter()
