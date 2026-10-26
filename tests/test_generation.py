"""Generation providers, offline: the HTTP client runs against httpx.MockTransport."""

import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings
from app.rag.generation import (
    ChatMessage,
    FakeGenerator,
    GenerationUnavailableError,
    OpenAICompatibleGenerator,
    create_generator,
    extractive_reply,
)
from app.rag.tokens import HuggingFaceTokenCounter, Utf8ByteCounter, create_token_counter

MESSAGES = [ChatMessage("system", "rules"), ChatMessage("user", '{"question": "q"}')]


def _generator(handler, **kwargs) -> OpenAICompatibleGenerator:
    return OpenAICompatibleGenerator(
        "http://llm.local/v1", "test-model", transport=httpx.MockTransport(handler), **kwargs
    )


def _completion(content) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_openai_compatible_request_and_reply() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _completion('{"status": "answered"}')

    reply = _generator(handler, api_key="sk-test", max_output_tokens=64).generate(MESSAGES)

    assert reply.text == '{"status": "answered"}'
    assert reply.prompt_tokens is None  # no usage block in this response
    [request] = seen
    assert str(request.url) == "http://llm.local/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["model"] == "test-model"
    assert body["messages"] == [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": '{"question": "q"}'},
    ]
    assert (body["temperature"], body["max_tokens"], body["stream"]) == (0, 64, False)
    assert body["response_format"] == {"type": "json_object"}


def test_json_mode_can_be_disabled_and_no_key_sends_no_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _completion("{}")

    _generator(handler, json_mode=False).generate(MESSAGES)
    assert "response_format" not in json.loads(seen[0].content)
    assert "Authorization" not in seen[0].headers


def test_null_content_is_an_empty_reply() -> None:
    assert _generator(lambda _: _completion(None)).generate(MESSAGES).text == ""


@pytest.mark.parametrize(
    ("usage", "expected"),
    [({"prompt_tokens": 2050, "completion_tokens": 5}, 2050), ({"prompt_tokens": "x"}, None),
     ({"prompt_tokens": True}, None), ("garbage", None)],
)
def test_prompt_tokens_are_read_from_usage(usage, expected) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "{}"}}], "usage": usage}
        )

    assert _generator(handler).generate(MESSAGES).prompt_tokens == expected


def _refuse(_: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused to http://llm.local SECRET")


def _timeout(_: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out")


@pytest.mark.parametrize(
    "handler",
    [
        _refuse,
        _timeout,
        lambda _: httpx.Response(500, text="model crashed while reading SECRET prompt"),
        lambda _: httpx.Response(404, json={"error": "model not found"}),
        lambda _: httpx.Response(302, headers={"Location": "http://elsewhere/"}),
        lambda _: httpx.Response(200, text="<html>proxy page</html>"),
        lambda _: httpx.Response(200, json={"choices": []}),
        lambda _: httpx.Response(200, json={"choices": [{"message": {"content": 42}}]}),
    ],
    ids=["refused", "timeout", "500", "404", "redirect", "html", "no-choices", "non-string"],
)
def test_provider_failures_raise_unavailable(handler, caplog) -> None:
    with pytest.raises(GenerationUnavailableError) as exc_info:
        _generator(handler).generate(MESSAGES)
    assert "SECRET" not in str(exc_info.value)
    assert all("SECRET" not in r.getMessage() for r in caplog.records)


def test_fake_generator_records_calls_and_is_deterministic() -> None:
    user = ChatMessage(
        "user",
        json.dumps(
            {"question": "q", "sources": [{"id": "S1", "text": "First one. Second one."}]}
        ),
    )
    generator = FakeGenerator()
    first = generator.generate([MESSAGES[0], user])
    assert first == generator.generate([MESSAGES[0], user])
    assert first.prompt_tokens is None
    assert json.loads(first.text) == {
        "status": "answered",
        "answer": "First one.",
        "citations": [{"source_id": "S1", "quote": "First one."}],
    }
    assert len(generator.calls) == 2


def test_extractive_reply_without_sources_abstains() -> None:
    reply = extractive_reply([ChatMessage("user", json.dumps({"question": "q", "sources": []}))])
    assert json.loads(reply)["status"] == "insufficient_evidence"


def test_factory() -> None:
    base = Settings(_env_file=None, environment="test")
    assert create_generator(base) is None
    assert isinstance(
        create_generator(base.model_copy(update={"generation_backend": "fake"})), FakeGenerator
    )
    remote = create_generator(
        base.model_copy(update={"generation_backend": "openai-compatible", "generation_model": "m"})
    )
    assert isinstance(remote, OpenAICompatibleGenerator)
    assert remote.model_name == "m"
    remote.close()
    with pytest.raises(ValueError, match="not allowed in prod"):
        create_generator(
            Settings(
                _env_file=None, environment="prod", generation_backend="fake",
                jwt_secret_key="x" * 32, allowed_hosts="testserver",
            )
        )


def test_byte_counter_is_an_upper_bound_on_characters() -> None:
    counter = Utf8ByteCounter()
    assert counter.count("abc") == 3
    assert counter.count("срок") == 8  # Cyrillic: 2 bytes per letter
    assert counter.count("ұзақ") == 8  # Kazakh letters too
    assert counter.count("") == 0


def test_token_counter_factory_and_unavailable_tokenizer(monkeypatch) -> None:
    base = Settings(_env_file=None, environment="test")
    assert isinstance(create_token_counter(base), Utf8ByteCounter)
    counter = create_token_counter(
        base.model_copy(update={"generation_tokenizer": "no-such/tokenizer"})
    )
    assert isinstance(counter, HuggingFaceTokenCounter)
    assert counter.name == "hf:no-such/tokenizer"

    class MissingTokenizer:
        @staticmethod
        def from_pretrained(name: str):
            raise OSError(f"{name} is not a valid model identifier")

    # A stand-in module: the test needs neither transformers nor the network.
    fake_module = SimpleNamespace(AutoTokenizer=MissingTokenizer)
    monkeypatch.setitem(sys.modules, "transformers", fake_module)
    with pytest.raises(GenerationUnavailableError):
        counter.count("text")


def test_hugging_face_counter_counts_without_special_tokens(monkeypatch) -> None:
    calls = []

    class WordTokenizer:
        def encode(self, text: str, add_special_tokens: bool) -> list[int]:
            calls.append(add_special_tokens)
            return list(range(len(text.split())))

    class Auto:
        @staticmethod
        def from_pretrained(name: str):
            return WordTokenizer()

    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoTokenizer=Auto))
    counter = HuggingFaceTokenCounter("any")
    assert counter.count("three word text") == 3
    assert calls == [False]


def test_extractive_fake_does_not_judge_support_checks() -> None:
    # Regression: the default fake crashed (KeyError) on the support check's prompts.
    verifier_call = [
        ChatMessage("system", "check"),
        ChatMessage("user", json.dumps({"question": "q", "evidence": [{"id": "S1", "text": "t"}]})),
    ]
    assert extractive_reply(verifier_call) == "{}"
