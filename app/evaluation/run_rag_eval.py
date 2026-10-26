"""RAG evaluation CLI: ask every labeled question through the production /ask service and
report retrieval coverage, abstention behavior, citation validity and latency.

    python -m app.evaluation.run_rag_eval --generator fake --offline
    python -m app.evaluation.run_rag_eval --generator configured   # DOCMIND_GENERATION_*

Two kinds of results, never mixed:
- Pipeline checks, valid with any generator: retrieval coverage (is a gold passage in the
  model's context?), owner isolation, and the citation cases (scripted replies that the
  validator must accept or reject with the expected reason).
- Generator quality (only with --generator configured, i.e. a real model): answer and
  abstention rates, unsupported-answer rate, citation id validity and quote verification
  of the raw replies, injection resistance, lexical answer proxies. With the fake
  generator these are reported as null: its extractive reply always cites S1 whatever the
  question, so any such number would describe the fake, not a model.
Citation-id validity is not faithfulness: a valid citation can still be misused (see
app/rag/grounding.py).

Isolation and database guard: same as run_search_eval (private schema in a transaction
that is rolled back, database name must end in "_test").
"""

import argparse
import json
import logging
import os
import statistics
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.embeddings.base import KNOWN_MODELS, Embedder
from app.embeddings.factory import create_embedder
from app.evaluation.dataset import LANGUAGES
from app.evaluation.rag_dataset import DEFAULT_RAG_DATASET, RagDataset, load_rag_dataset
from app.evaluation.run_search_eval import (
    TEST_DB_ENV,
    EvaluationError,
    _database_versions,
    _library_versions,
    _populate,
    document_uuid,
    isolated_schema,
    require_test_database,
)
from app.models import User
from app.rag.context import GenerationContext
from app.rag.generation import (
    ChatMessage,
    FakeGenerator,
    Generation,
    Generator,
    create_generator,
)
from app.rag.grounding import GROUNDING_POLICY, Abstention, locate_quote, parse_reply
from app.rag.prompt import PROMPT_VERSION
from app.rag.tokens import TokenCounter, create_token_counter
from app.schemas.ask import AskRequest
from app.services import ask as ask_service

logger = logging.getLogger("app.evaluation")

GENERATORS = ("fake", "configured")


class RecordingGenerator:
    """Passes calls through and keeps the raw replies, to analyze them before validation."""

    def __init__(self, inner: Generator) -> None:
        self.inner = inner
        self.model_name = inner.model_name
        self.replies: list[str] = []

    def generate(self, messages: Sequence[ChatMessage]) -> Generation:
        generation = self.inner.generate(messages)
        self.replies.append(generation.text)
        return generation

    def close(self) -> None:
        self.inner.close()


def _sources(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    return json.loads(messages[-1].content)["sources"]


def _first_sentence(text: str) -> str:
    end = text.find(". ")
    return text[: end + 1] if end > 0 else text


def _answer(answer: str, *citations: tuple[str, str]) -> str:
    return json.dumps(
        {
            "status": "answered",
            "answer": answer,
            "citations": [{"source_id": s, "quote": q} for s, q in citations],
        },
        ensure_ascii=False,
    )


def scripted_reply(kind: str) -> Callable[[Sequence[ChatMessage]], str]:
    """The reply a (misbehaving) model would give for each citation case."""

    def respond(messages: Sequence[ChatMessage]) -> str:
        sources = _sources(messages)
        s1 = _first_sentence(sources[0]["text"])
        s2 = _first_sentence(sources[1]["text"]) if len(sources) > 1 else "no second source"
        replies = {
            "valid-exact-quote": _answer(s1, ("S1", s1)),
            "valid-quote-ru": _answer(s1, ("S1", s1)),
            "valid-quote-kk": _answer(s1, ("S1", s1)),
            "duplicate-citations": _answer(s1, ("S1", s1), ("S1", s1)),
            "fabricated-source-id": _answer(s1, ("S99", s1)),
            "chunk-uuid-as-source": _answer(s1, (str(uuid.uuid4()), s1)),
            "marker-outside-context": _answer(s1 + " [S42]", ("S1", s1)),
            "quote-not-in-source": _answer(
                s1, ("S1", "This sentence does not occur in any document.")
            ),
            "quote-from-other-source": _answer(s2, ("S1", s2)),
            "invented-number": _answer("It takes 987654 days.", ("S1", s1)),
            "no-citations": _answer(s1),
            "malformed-json": "Answer: " + s1,
            "empty-output": "",
            "model-abstains": json.dumps(
                {"status": "insufficient_evidence", "answer": "", "citations": []}
            ),
        }
        return replies[kind]

    return respond


def _contains_any(text: str, needles: Sequence[str]) -> bool:
    lowered = text.casefold()
    return any(needle.casefold() in lowered for needle in needles)


def _raw_citation_stats(raw: str, context: GenerationContext) -> dict[str, int | bool]:
    """Citation validity of the reply as the model wrote it, before any rejection."""
    parsed = parse_reply(raw)
    if isinstance(parsed, Abstention):
        return {"parsed": False, "citations": 0, "valid_ids": 0, "verified_quotes": 0}
    sources = context.by_id()
    valid = [c for c in parsed.citations if c.source_id.strip() in sources]
    verified = [
        c for c in valid if locate_quote(c.quote, sources[c.source_id.strip()].content)
    ]
    return {
        "parsed": True,
        "citations": len(parsed.citations),
        "valid_ids": len(valid),
        "verified_quotes": len(verified),
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _latency(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"median": None, "max": None, "n": 0}
    return {
        "median": round(statistics.median(values), 1),
        "max": round(max(values), 1),
        "n": len(values),
    }


def aggregate(records: list[dict[str, Any]], real: bool) -> dict[str, Any]:
    with_gold = [r for r in records if r["gold"]]
    injected = [r for r in records if r["injected_in_context"] is not None]
    summary: dict[str, Any] = {
        "questions": len(records),
        "retrieval_coverage": _ratio(
            sum(r["gold_in_context"] for r in with_gold), len(with_gold)
        ),
        # Pipeline fact: did the injected passage actually reach the model?
        "injection_in_context": _ratio(
            sum(bool(r["injected_in_context"]) for r in injected), len(injected)
        ),
        "latency_ms": {"retrieval": _latency([r["retrieval_ms"] for r in records])},
    }
    if not real:
        # Outcomes and generation latency of the fake describe the fake, not a model.
        summary["outcomes"] = None
        summary["generator_quality"] = None
        return summary
    summary["outcomes"] = dict(sorted(Counter(r["outcome"] for r in records).items()))
    summary["latency_ms"] |= {
        "generation": _latency(
            [r["generation_ms"] for r in records if r["generation_ms"] is not None]
        ),
        "total": _latency([r["total_ms"] for r in records]),
    }

    expect_answer = [r for r in records if r["expects_answer"]]
    expect_abstain = [r for r in records if not r["expects_answer"]]
    answered = [r for r in expect_answer if r["outcome"] == "answered"]
    injections = [r for r in records if r["injected_in_context"]]
    raw = [r["raw"] for r in records if r["raw"] is not None]
    summary["generator_quality"] = {
        "answer_rate_when_answerable": _ratio(len(answered), len(expect_answer)),
        "abstention_rate_when_unanswerable": _ratio(
            sum(r["outcome"] != "answered" for r in expect_abstain), len(expect_abstain)
        ),
        # Answers shown to the user although the corpus does not answer the question.
        "unsupported_answer_rate": _ratio(
            sum(r["outcome"] == "answered" for r in expect_abstain), len(expect_abstain)
        ),
        "gold_cited_rate_when_answered": _ratio(
            sum(r["gold_cited"] for r in answered), len(answered)
        ),
        "answer_contains_expected_rate_when_answered": _ratio(
            sum(r["answer_contains_expected"] for r in answered), len(answered)
        ),
        # Only over injections that reached the context: the others were never tested.
        "injection_followed_rate": _ratio(
            sum(bool(r["answer_contains_forbidden"]) for r in injections),
            len(injections),
        ),
        "raw_reply_contract_rate": _ratio(sum(s["parsed"] for s in raw), len(raw)),
        "raw_citation_id_validity": _ratio(
            sum(s["valid_ids"] for s in raw), sum(s["citations"] for s in raw)
        ),
        "raw_quote_verification": _ratio(
            sum(s["verified_quotes"] for s in raw), sum(s["valid_ids"] for s in raw)
        ),
    }
    return summary


def _ask(
    session: Session,
    embedder: Embedder,
    generator: Generator,
    counter: TokenCounter,
    settings: Settings,
    owner: User,
    question: str,
    top_k: int,
    document_ids: list[uuid.UUID] | None,
) -> ask_service.AskOutcome:
    request = AskRequest(question=question, top_k=top_k, document_ids=document_ids)
    return ask_service.ask(session, embedder, generator, counter, settings, owner, request)


def evaluate(
    engine: Engine,
    dataset: RagDataset,
    embedder: Embedder,
    generator: Generator,
    counter: TokenCounter,
    settings: Settings,
    *,
    real_generator: bool,
    top_k: int = 5,
) -> dict[str, Any]:
    corpus = dataset.corpus
    passage_of = {
        (document_uuid(doc.id), p.index): p.id for doc in corpus.documents for p in doc.passages
    }
    docs_by_language = {
        lang: [document_uuid(d.id) for d in corpus.documents if d.language == lang]
        for lang in LANGUAGES
    }
    own = {document_uuid(d.id) for d in corpus.documents}
    recorder = RecordingGenerator(generator)
    records: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    with isolated_schema(engine, embedder.dimension) as conn:
        # SAVEPOINT mode: the service ends its read transaction with rollback(), which must
        # not roll back the private schema and its data.
        session = Session(bind=conn, join_transaction_mode="create_savepoint")
        owner = _populate(session, corpus, embedder, settings)
        session.commit()

        for question in dataset.questions:
            before = len(recorder.replies)
            outcome = _ask(
                session, embedder, recorder, counter, settings, owner, question.text, top_k,
                docs_by_language[question.language],
            )
            context = outcome.context
            in_context = (
                {passage_of[(s.document_id, s.chunk_index)] for s in context.sources}
                if context
                else set()
            )
            answered = not isinstance(outcome.result, Abstention)
            cited = (
                {
                    passage_of[(c.source.document_id, c.source.chunk_index)]
                    for c in outcome.result.citations
                }
                if answered
                else set()
            )
            answer = outcome.result.answer if answered else None
            raw = recorder.replies[before] if len(recorder.replies) > before else None
            records.append(
                {
                    "id": question.id,
                    "language": question.language,
                    "category": question.category,
                    "expects_answer": question.expects_answer,
                    "outcome": "answered" if answered else outcome.result.reason.value,
                    "gold": sorted(question.gold),
                    "context": sorted(in_context),
                    "gold_in_context": bool(question.gold & in_context),
                    "cited": sorted(cited),
                    "gold_cited": bool(question.gold & cited),
                    "injected_in_context": bool(question.injected & in_context)
                    if question.injected
                    else None,
                    "answer": answer,
                    "answer_contains_expected": _contains_any(answer, question.answer_contains)
                    if answer and question.answer_contains
                    else None,
                    "answer_contains_forbidden": _contains_any(answer, question.forbidden)
                    if answer and question.forbidden
                    else None,
                    "raw": _raw_citation_stats(raw, context)
                    if raw is not None and context is not None
                    else None,
                    "retrieval_ms": outcome.retrieval_ms,
                    "generation_ms": outcome.generation_ms,
                    "total_ms": outcome.total_ms,
                }
            )

        questions = {q.id: q for q in dataset.questions}
        for case in dataset.citation_cases:
            question = questions[case.question]
            outcome = _ask(
                session, embedder, FakeGenerator(scripted_reply(case.id)), counter, settings,
                owner, question.text, top_k, docs_by_language[question.language],
            )
            got = (
                outcome.result.reason.value
                if isinstance(outcome.result, Abstention)
                else "answered"
            )
            cases.append(
                {"id": case.id, "expect": case.expect, "got": got, "passed": got == case.expect}
            )

        # Owner isolation: unfiltered questions never put the stranger's copies in context.
        for question in dataset.questions:
            outcome = _ask(
                session, embedder, FakeGenerator(), counter, settings, owner, question.text,
                top_k, None,
            )
            if outcome.context and any(s.document_id not in own for s in outcome.context.sources):
                raise EvaluationError("Another user's chunk reached the generation context")
        session.close()

    return {
        "overall": aggregate(records, real_generator),
        "by_language": {
            lang: aggregate([r for r in records if r["language"] == lang], real_generator)
            for lang in LANGUAGES
        },
        "by_category": {
            category: aggregate(
                [r for r in records if r["category"] == category], real_generator
            )
            for category in sorted({r["category"] for r in records})
        },
        "citation_cases": {
            "passed": sum(c["passed"] for c in cases),
            "total": len(cases),
            "cases": cases,
        },
        "questions": records,
    }


def run(
    engine: Engine,
    dataset: RagDataset,
    embedder: Embedder,
    generator: Generator,
    counter: TokenCounter,
    settings: Settings,
    *,
    generator_kind: str,
    top_k: int,
    offline: bool,
) -> dict[str, Any]:
    real = generator_kind == "configured"
    started = time.perf_counter()
    results = evaluate(
        engine, dataset, embedder, generator, counter, settings, real_generator=real, top_k=top_k
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": {
            "name": dataset.name,
            "version": dataset.version,
            "sha256": dataset.sha256,
            "questions": len(dataset.questions),
            "citation_cases": len(dataset.citation_cases),
            "passages": len(dataset.corpus.passages()),
        },
        "generator": {
            "kind": "real" if real else "fake",
            "backend": settings.generation_backend if real else "fake",
            "model": generator.model_name,
            "note": None
            if real
            else "Deterministic extractive fake: no generator-quality numbers are reported. "
            "No real-model measurement was performed in this run.",
        },
        "settings": {
            "top_k": top_k,
            "retrieval": "semantic (app.services.search.search, POST /api/v1/search)",
            "query_scope": "documents of the question's language (document_ids filter)",
            "embedding_model": embedder.model_name,
            "context_token_budget": settings.rag_context_token_budget,
            "token_counter": counter.name,
            "grounding_policy": GROUNDING_POLICY,
            "prompt_version": PROMPT_VERSION,
            "hf_hub_offline": offline,
        },
        "environment": {"libraries": _library_versions(), **_database_versions(engine)},
        "run_seconds": round(time.perf_counter() - started, 2),
        "results": results,
    }


def _fmt(value: Any) -> str:
    return "n/a" if value is None else f"{value:.3f}" if isinstance(value, float) else str(value)


def render_markdown(report: dict[str, Any]) -> str:
    results = report["results"]
    generator = report["generator"]
    lines = [
        f"Generator: {generator['kind']} ({generator['model']})",
        f"Embedding model: {report['settings']['embedding_model']}, top_k "
        f"{report['settings']['top_k']}",
        "",
        "| Scope | Questions | Retrieval coverage | Injection in context | Outcomes |",
        "|---|---|---|---|---|",
    ]
    scopes = {"overall": results["overall"], **results["by_language"], **results["by_category"]}
    for scope, summary in scopes.items():
        outcomes = (
            ", ".join(f"{k}={v}" for k, v in summary["outcomes"].items())
            if summary["outcomes"] is not None
            else "not reported (fake generator)"
        )
        lines.append(
            f"| {scope} | {summary['questions']} | {_fmt(summary['retrieval_coverage'])} "
            f"| {_fmt(summary['injection_in_context'])} | {outcomes} |"
        )
    quality = results["overall"]["generator_quality"]
    lines.append("")
    if quality is None:
        lines.append(f"Generator quality: not measured. {generator['note']}")
    else:
        lines.extend(f"- {name}: {_fmt(value)}" for name, value in quality.items())
    cases = results["citation_cases"]
    lines.append("")
    lines.append(f"Citation validation cases: {cases['passed']}/{cases['total']} as expected")
    lines.extend(
        f"  FAILED {c['id']}: expected {c['expect']}, got {c['got']}"
        for c in cases["cases"]
        if not c["passed"]
    )
    latency = results["overall"]["latency_ms"]
    parts = [
        f"{name} median {_fmt(values['median'])} (max {_fmt(values['max'])})"
        for name, values in latency.items()
    ]
    lines.append("Latency (ms, single run, indicative): " + ", ".join(parts))
    misses = [q for q in results["questions"] if q["gold"] and not q["gold_in_context"]]
    lines.append(f"Questions whose gold passage was not in the context: {len(misses)}")
    lines.extend(f"  {q['id']}: context {q['context']}" for q in misses)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.evaluation.run_rag_eval", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "--generator",
        choices=GENERATORS,
        default="fake",
        help="fake = deterministic pipeline check; configured = DOCMIND_GENERATION_* model",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_RAG_DATASET)
    parser.add_argument("--output", type=Path, default=None, help="JSON report path")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="set HF_HUB_OFFLINE=1: no Hugging Face network calls, cached models only",
    )
    args = parser.parse_args(argv)

    if args.offline:
        if "huggingface_hub" in sys.modules:
            parser.error("--offline must be set before huggingface_hub is imported")
        os.environ["HF_HUB_OFFLINE"] = "1"
    settings = Settings()
    try:
        if not 1 <= args.top_k <= 20:
            raise ValueError("--top-k must be between 1 and 20")
        url = os.environ.get(TEST_DB_ENV)
        if not url:
            raise EvaluationError(f"Set {TEST_DB_ENV} to a database whose name ends in '_test'")
        require_test_database(url)
        if args.generator == "configured" and settings.generation_backend in ("disabled", "fake"):
            raise ValueError(
                "--generator configured needs DOCMIND_GENERATION_BACKEND=openai-compatible"
            )
    except (ValueError, EvaluationError) as exc:
        parser.error(str(exc))

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for name in ("app.services.search", "app.services.ask", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)

    dataset = load_rag_dataset(args.dataset)
    spec = KNOWN_MODELS[settings.embedding_model]
    embedder = create_embedder(
        settings.model_copy(update={"embedding_backend": "sentence-transformers"}), spec.dimension
    )
    embedder.load()  # type: ignore[attr-defined]
    if args.generator == "configured":
        generator = create_generator(settings)
        assert generator is not None
    else:
        generator = FakeGenerator()
    counter = create_token_counter(settings)
    engine = create_engine(url)
    try:
        report = run(
            engine, dataset, embedder, generator, counter, settings,
            generator_kind=args.generator, top_k=args.top_k, offline=args.offline,
        )
    finally:
        engine.dispose()
        generator.close()

    suffix = "fake" if args.generator == "fake" else "real"
    output = args.output or Path("docs/evaluation") / f"rag_eval_v{dataset.version}_{suffix}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(render_markdown(report))
    print(f"\nReport written to {output}")
    return 0 if report["results"]["citation_cases"]["passed"] == len(dataset.citation_cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
