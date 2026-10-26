"""Generation providers: turn chat messages into the model's raw reply text.

A provider only transports text. It never parses or trusts the reply: the output contract
and every citation are checked afterwards (app/rag/grounding.py).

Two kinds of failure are kept apart:
- the provider cannot produce a reply at all (server down, timeout, HTTP error, a body that
  is not a chat completion): GenerationUnavailableError, reported as 503;
- the provider replies, but the reply is empty or breaks the contract: that is the model's
  answer, and the pipeline abstains (200, abstained=true).
"""

import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


class GenerationUnavailableError(Exception):
    """The provider could not produce a reply. The message never contains prompt text."""


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class Generation:
    text: str
    # Prompt size as counted by the server (OpenAI "usage.prompt_tokens"), if reported.
    # Lets the caller detect a prompt the server silently truncated.
    prompt_tokens: int | None = None


class Generator(Protocol):
    model_name: str

    def generate(self, messages: Sequence[ChatMessage]) -> Generation:
        """Return the model's raw reply; raise GenerationUnavailableError on failure."""
        ...

    def close(self) -> None: ...


class OpenAICompatibleGenerator:
    """Client for the /chat/completions endpoint shared by Ollama, llama.cpp server,
    LM Studio, vLLM and hosted APIs. Temperature 0 for repeatable answers."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout_s: float = 120.0,
        max_output_tokens: int = 512,
        json_mode: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model_name = model
        self._max_output_tokens = max_output_tokens
        self._json_mode = json_mode
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=timeout_s,
            transport=transport,
            # Never follow redirects with the API key and the prompt.
            follow_redirects=False,
        )

    def generate(self, messages: Sequence[ChatMessage]) -> Generation:
        payload: dict = {
            "model": self.model_name,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": 0,
            "max_tokens": self._max_output_tokens,
            "stream": False,
        }
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            response = self._client.post("chat/completions", json=payload)
        except httpx.HTTPError as exc:
            # Class name only: messages can include the URL and, for some errors, the body.
            logger.warning("Generation request failed: %s", type(exc).__name__)
            raise GenerationUnavailableError(type(exc).__name__) from None
        if response.status_code != 200:
            # The body is not logged: servers may echo the prompt in error messages.
            logger.warning("Generation server returned HTTP %d", response.status_code)
            raise GenerationUnavailableError(f"HTTP {response.status_code}")
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            logger.warning("Generation server returned a body that is not a chat completion")
            raise GenerationUnavailableError("invalid chat completion body") from None
        # null content (e.g. a refusal or a tool call) is an empty reply, not an outage.
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise GenerationUnavailableError("invalid chat completion body")
        usage = body.get("usage")
        prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        if not isinstance(prompt_tokens, int) or isinstance(prompt_tokens, bool):
            prompt_tokens = None
        return Generation(content, prompt_tokens)

    def close(self) -> None:
        self._client.close()


Responder = Callable[[Sequence[ChatMessage]], str]

_SENTENCE = re.compile(r".+?(?:[.!?](?=\s)|$)", re.DOTALL)


def extractive_reply(messages: Sequence[ChatMessage]) -> str:
    """Answer with the first sentence of source S1, quoted verbatim. Says nothing about
    whether S1 answers the question: it exists to exercise the pipeline, not to answer.

    Calls that are not answer prompts (the support check's, which carry "evidence") get an
    empty object: the fake cannot judge support, so strict mode fails closed
    (verification_inconclusive) instead of crashing."""
    data = json.loads(messages[-1].content)
    if "sources" not in data:
        return "{}"
    sources = data["sources"]
    if not sources:
        return json.dumps({"status": "insufficient_evidence", "answer": "", "citations": []})
    first = sources[0]
    match = _SENTENCE.match(first["text"].strip())
    sentence = match.group(0).strip() if match else first["text"].strip()
    return json.dumps(
        {
            "status": "answered",
            "answer": sentence,
            "citations": [{"source_id": first["id"], "quote": sentence}],
        },
        ensure_ascii=False,
    )


class FakeGenerator:
    """Deterministic, offline stand-in for tests and development without a model.

    The default responder is extractive_reply; tests pass their own to script any reply,
    including malformed or hallucinated ones. Records every call for assertions.
    """

    def __init__(self, responder: Responder = extractive_reply, model_name: str = "") -> None:
        self.model_name = model_name or "fake/extractive"
        self._responder = responder
        self.calls: list[list[ChatMessage]] = []

    def generate(self, messages: Sequence[ChatMessage]) -> Generation:
        self.calls.append(list(messages))
        return Generation(self._responder(messages))

    def close(self) -> None:
        pass


def create_generator(settings: Settings) -> Generator | None:
    """The configured provider, or None when generation is disabled."""
    if settings.generation_backend == "disabled":
        return None
    if settings.generation_backend == "fake":
        if settings.environment == "prod":
            raise ValueError("The fake generation backend is not allowed in prod")
        return FakeGenerator()
    api_key = settings.generation_api_key
    return OpenAICompatibleGenerator(
        settings.generation_base_url,
        settings.generation_model,
        api_key=api_key.get_secret_value() if api_key else None,
        timeout_s=settings.generation_timeout_s,
        max_output_tokens=settings.generation_max_output_tokens,
        json_mode=settings.generation_json_mode,
    )
