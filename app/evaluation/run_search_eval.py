"""Search-quality evaluation CLI: embed the labeled corpus with each model, run every query
through the production search service, and report Recall@k, Precision@k and MRR.

    python -m app.evaluation.run_search_eval --models current,bge-m3 --k 1,3,5
    python -m app.evaluation.run_search_eval --models current --offline

Needs DOCMIND_TEST_DATABASE_URL (database name ending in "_test", pgvector enabled, e.g.
after the integration tests or `alembic upgrade head` against it).

Isolation: everything happens inside ONE transaction that is never committed. It creates
a private schema (docmind_eval_<random>) with the application's tables, sized to the
model's vector dimension, and the final ROLLBACK removes it (PostgreSQL DDL is
transactional), even if the process dies. Existing schemas and data are never touched.

Consistency with the API: queries go through app.services.search.search, so they get the
same validation, query prefix, normalization, cosine distance, tie-breaking and
owner/model filtering as POST /api/v1/search. Each query is restricted (document_ids) to
the documents of its own language, matching how the dataset is labeled.
"""

import argparse
import hashlib
import importlib.metadata
import json
import logging
import os
import platform
import statistics
import sys
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.embeddings.base import KNOWN_MODELS, Embedder
from app.embeddings.factory import create_embedder
from app.evaluation.dataset import DEFAULT_DATASET, LANGUAGES, EvalDataset, load_dataset
from app.evaluation.metrics import aggregate, first_relevant_rank, query_metrics
from app.ingestion.chunking import Chunker
from app.models import EMBEDDING_DIMENSION, Base, Document, DocumentChunk, User
from app.schemas.search import MAX_TOP_K, HybridSearchRequest, SearchRequest
from app.services import search as search_service

logger = logging.getLogger("app.evaluation")

TEST_DB_ENV = "DOCMIND_TEST_DATABASE_URL"
MODEL_ALIASES = {"e5-small": "intfloat/multilingual-e5-small", "bge-m3": "BAAI/bge-m3"}
# "semantic" is POST /api/v1/search (Phase 5); the others go through POST /search/hybrid.
RETRIEVALS = ("semantic", "full_text", "hybrid")
_HYBRID_MODES = {"full_text": "full_text", "hybrid": "hybrid"}
# Fixed namespace: document UUIDs (and so the search tie-breaker) are the same every run.
_NAMESPACE = uuid.UUID("5d0c7c1e-6a0e-4c1e-9f8a-2f4b8e6d1a00")
_LIBRARIES = ("torch", "transformers", "sentence-transformers", "huggingface-hub", "pgvector")


class EvaluationError(RuntimeError):
    """The run cannot produce trustworthy numbers (setup or consistency problem)."""


def document_uuid(document_id: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, document_id)


def require_test_database(url: str) -> str:
    database = make_url(url).database or ""
    if not database.endswith("_test"):
        # Same guard as the integration tests: never point evaluation at real data.
        raise EvaluationError(
            f"Refusing to run: database name must end with '_test' ({database!r})"
        )
    return url


def resolve_models(spec: str, current_model: str) -> list[str]:
    models = []
    for name in (part.strip() for part in spec.split(",") if part.strip()):
        model = current_model if name == "current" else MODEL_ALIASES.get(name, name)
        if model not in KNOWN_MODELS:
            raise ValueError(f"Unknown model {name!r}; use current, {', '.join(MODEL_ALIASES)}")
        if model not in models:
            models.append(model)
    if not models:
        raise ValueError("No models given")
    return models


def parse_retrievals(spec: str) -> list[str]:
    retrievals = list(dict.fromkeys(part.strip() for part in spec.split(",") if part.strip()))
    unknown = [r for r in retrievals if r not in RETRIEVALS]
    if not retrievals or unknown:
        raise ValueError(f"Retrieval methods must be among {', '.join(RETRIEVALS)}")
    return retrievals


def parse_ks(spec: str) -> list[int]:
    try:
        ks = sorted({int(part) for part in spec.split(",") if part.strip()})
    except ValueError:
        raise ValueError(f"Invalid k list {spec!r}") from None
    if not ks or ks[0] < 1 or ks[-1] > MAX_TOP_K:
        raise ValueError(f"k values must be between 1 and {MAX_TOP_K}")
    return ks


@contextmanager
def isolated_schema(engine: Engine, dimension: int) -> Iterator[Connection]:
    """A throwaway schema with the app's tables, inside a transaction that is rolled back."""
    schema = f"docmind_eval_{uuid.uuid4().hex[:12]}"  # generated, never user input
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            if conn.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")) is None:
                raise EvaluationError(
                    "pgvector is not enabled in the test database; run the integration tests "
                    "or `alembic upgrade head` against it first"
                )
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            translated = conn.execution_options(schema_translate_map={None: schema})
            Base.metadata.create_all(translated)
            if dimension != EMBEDDING_DIMENSION:
                # Only in this private schema: the production column stays vector(384).
                conn.execute(
                    text(
                        f'ALTER TABLE "{schema}".document_chunks '
                        f"ALTER COLUMN embedding TYPE vector({int(dimension)})"
                    )
                )
            yield translated
        finally:
            transaction.rollback()


def _check_single_chunks(dataset: EvalDataset, embedder: Embedder, settings: Settings) -> dict:
    """Each passage must be exactly one chunk under the production chunker and tokenizer."""
    if settings.chunk_size_tokens > embedder.max_input_tokens:
        raise EvaluationError("chunk_size_tokens exceeds the model input limit")
    chunker = Chunker(embedder.tokenizer, settings.chunk_size_tokens, settings.chunk_overlap_tokens)
    token_counts = {}
    for passage in dataset.passages().values():
        chunks = list(chunker.chunk_pages([(None, passage.text)]))
        if len(chunks) != 1 or chunks[0].text != passage.text:
            raise EvaluationError(f"Passage {passage.id} does not map to exactly one chunk")
        token_counts[passage.id] = chunks[0].token_count
    return token_counts


def _populate(
    session: Session, dataset: EvalDataset, embedder: Embedder, settings: Settings
) -> User:
    token_counts = _check_single_chunks(dataset, embedder, settings)
    owner = User(email="eval-owner@docmind.invalid")
    # Owns a copy of every passage; must never show up in the owner's results.
    stranger = User(email="eval-stranger@docmind.invalid")
    session.add_all([owner, stranger])
    session.flush()

    passages = list(dataset.passages().values())
    vectors = []
    batch = settings.embedding_batch_size
    for start in range(0, len(passages), batch):
        vectors.extend(embedder.embed_passages([p.text for p in passages[start : start + batch]]))
    vector_of = {p.id: v for p, v in zip(passages, vectors, strict=True)}

    rows = []
    for user, prefix in ((owner, ""), (stranger, "stranger-")):
        for doc in dataset.documents:
            body = "\n\n".join(p.text for p in doc.passages).encode()
            document_id = document_uuid(prefix + doc.id)
            session.add(
                Document(
                    id=document_id,
                    user_id=user.id,
                    filename=f"{doc.id}.txt",
                    content_type="text/plain",
                    file_size=len(body),
                    content_hash=hashlib.sha256(body).hexdigest(),
                )
            )
            rows.extend(
                {
                    "document_id": document_id,
                    "chunk_index": p.index,
                    "content": p.text,
                    "page_number": None,
                    "char_start": 0,
                    "char_end": len(p.text),
                    "token_count": token_counts[p.id],
                    "embedding": vector_of[p.id],
                    "embedding_model": embedder.model_name,
                }
                for p in doc.passages
            )
    session.flush()
    session.execute(insert(DocumentChunk), rows)
    return owner


def _run_query(
    session: Session,
    embedder: Embedder,
    settings: Settings,
    owner: User,
    retrieval: str,
    text_: str,
    top_k: int,
    document_ids: list[uuid.UUID] | None,
) -> list[tuple[Any, dict[str, Any]]]:
    """One query through the production service; returns (hit, per-hit details) pairs."""
    if retrieval == "semantic":
        request = SearchRequest(query=text_, top_k=top_k, document_ids=document_ids)
        hits = search_service.search(session, embedder, settings, owner, request)
        return [(h, {"similarity": round(h.similarity, 4)}) for h in hits]
    hybrid_request = HybridSearchRequest(
        query=text_, top_k=top_k, document_ids=document_ids, mode=_HYBRID_MODES[retrieval]
    )
    outcome = search_service.hybrid_search(session, embedder, settings, owner, hybrid_request)
    return [
        (
            h,
            {
                "rrf_score": round(h.rrf_score, 6),
                "vector_rank": h.vector_rank,
                "full_text_rank": h.full_text_rank,
            },
        )
        for h in outcome.hits
    ]


def evaluate_model_retrievals(
    engine: Engine,
    dataset: EvalDataset,
    embedder: Embedder,
    settings: Settings,
    ks: Sequence[int],
    retrievals: Sequence[str] = ("semantic",),
) -> dict[str, dict[str, Any]]:
    """Embed the corpus once, then evaluate every retrieval method on it."""
    depth = max(ks)
    passage_by_key = {
        (document_uuid(doc.id), p.index): p.id for doc in dataset.documents for p in doc.passages
    }
    docs_by_language = {
        lang: [document_uuid(d.id) for d in dataset.documents if d.language == lang]
        for lang in LANGUAGES
    }
    own_documents = {document_uuid(d.id) for d in dataset.documents}
    results: dict[str, dict[str, Any]] = {}
    with isolated_schema(engine, embedder.dimension) as conn:
        session = Session(bind=conn)
        started = time.perf_counter()
        owner = _populate(session, dataset, embedder, settings)
        corpus_seconds = time.perf_counter() - started

        for retrieval in retrievals:
            queries, rows, latencies = [], [], []
            for query in dataset.queries:
                started = time.perf_counter()
                pairs = _run_query(
                    session, embedder, settings, owner, retrieval, query.text, depth,
                    docs_by_language[query.language],
                )
                latencies.append((time.perf_counter() - started) * 1000)
                hits = [h for h, _ in pairs]
                if any((h.document_id, h.chunk_index) not in passage_by_key for h in hits):
                    raise EvaluationError(f"Query {query.id} returned chunks outside the dataset")
                ranked = [passage_by_key[(h.document_id, h.chunk_index)] for h in hits]
                metrics = query_metrics(ranked, query.relevant, ks)
                rows.append((query.language, metrics))
                queries.append(
                    {
                        "id": query.id,
                        "language": query.language,
                        "difficulty": query.difficulty,
                        "query": query.text,
                        "relevant": sorted(query.relevant),
                        "first_relevant_rank": first_relevant_rank(ranked, query.relevant),
                        "retrieved": [
                            {"passage_id": pid, **details, "relevant": pid in query.relevant}
                            for pid, (_, details) in zip(ranked, pairs, strict=True)
                        ],
                        "metrics": metrics,
                    }
                )

            # Owner isolation sanity check: unfiltered searches never return the
            # stranger's copies of the same passages.
            for query in dataset.queries:
                pairs = _run_query(
                    session, embedder, settings, owner, retrieval, query.text, depth, None
                )
                if any(h.document_id not in own_documents for h, _ in pairs):
                    raise EvaluationError("Search returned another user's document")

            summary = aggregate(rows)
            results[retrieval] = {
                "status": "ok",
                "retrieval": retrieval,
                "metrics": {
                    "overall": summary["overall"],
                    "by_language": summary["by_group"],
                    "macro_language": summary["macro"],
                },
                "query_counts": summary["query_counts"],
                "timing": {
                    "note": "Single run, indicative only; not a benchmark.",
                    "corpus_embed_and_insert_seconds": round(corpus_seconds, 3),
                    "search_ms_median": round(statistics.median(latencies), 2),
                    "search_ms_max": round(max(latencies), 2),
                    "search_ms_n": len(latencies),
                },
                "queries": queries,
            }
        session.close()
    return results


def evaluate_model(
    engine: Engine,
    dataset: EvalDataset,
    embedder: Embedder,
    settings: Settings,
    ks: Sequence[int],
    retrieval: str = "semantic",
) -> dict[str, Any]:
    return evaluate_model_retrievals(engine, dataset, embedder, settings, ks, [retrieval])[
        retrieval
    ]


def _library_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in _LIBRARIES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _model_revision(model: str) -> str | None:
    """Commit hash of the cached model snapshot, read from the local cache (no network)."""
    try:
        from huggingface_hub import try_to_load_from_cache

        path = try_to_load_from_cache(model, "config.json")
    except Exception:
        return None
    if not isinstance(path, str):
        return None
    parts = Path(path).parts
    return parts[parts.index("snapshots") + 1] if "snapshots" in parts else None


def _database_versions(engine: Engine) -> dict[str, str | None]:
    with engine.connect() as conn:
        return {
            "postgresql": conn.scalar(text("SHOW server_version")),
            "pgvector": conn.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ),
        }


def comparison_rows(models: list[dict[str, Any]], ks: Sequence[int]) -> list[dict[str, Any]]:
    rows = []
    for model in models:
        if model["status"] != "ok":
            continue
        metrics = model["metrics"]
        scopes = {
            "overall": metrics["overall"],
            **metrics["by_language"],
            "macro_language": metrics["macro_language"],
        }
        for scope, values in scopes.items():
            cells = [(name, k) for name in ("recall", "precision") for k in ks]
            for name, k in [*cells, ("mrr", max(ks))]:
                key = "mrr" if name == "mrr" else f"{name}@{k}"
                rows.append(
                    {
                        "model": model["model"],
                        "retrieval": model.get("retrieval", "semantic"),
                        "scope": scope,
                        "metric": name,
                        "k": k,
                        "score": round(values[key], 4),
                    }
                )
    return rows


def run(
    engine: Engine,
    dataset: EvalDataset,
    models: list[str],
    ks: list[int],
    base_settings: Settings,
    *,
    offline: bool,
    retrievals: Sequence[str] = ("semantic",),
) -> dict[str, Any]:
    results = []
    for model in models:
        spec = KNOWN_MODELS[model]
        settings = base_settings.model_copy(
            update={"embedding_backend": "sentence-transformers", "embedding_model": model}
        )
        entry: dict[str, Any] = {
            "model": model,
            "dimension": spec.dimension,
            "query_prefix": spec.query_prefix,
            "passage_prefix": spec.passage_prefix,
        }
        try:
            started = time.perf_counter()
            embedder = create_embedder(settings, spec.dimension)
            # The real backend loads lazily; load now so a missing model is reported here.
            embedder.load()  # type: ignore[attr-defined]
            entry["load_seconds"] = round(time.perf_counter() - started, 2)
        except Exception as exc:
            # Report the blocker and keep going with the other models.
            logger.error("Model %s unavailable: %s", model, type(exc).__name__)
            entry |= {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"[:500]}
            results.extend(entry | {"retrieval": retrieval} for retrieval in retrievals)
            continue
        entry["revision"] = _model_revision(model)
        entry["device"] = getattr(embedder, "device", None)
        logger.info("Evaluating %s (%s)", model, ", ".join(retrievals))
        per_retrieval = evaluate_model_retrievals(
            engine, dataset, embedder, settings, ks, retrievals
        )
        results.extend(entry | per_retrieval[retrieval] for retrieval in retrievals)

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "dataset": {
            "name": dataset.name,
            "version": dataset.version,
            "sha256": dataset.sha256,
            "documents": len(dataset.documents),
            "passages": len(dataset.passages()),
            "queries": len(dataset.queries),
        },
        "settings": {
            "k": ks,
            "retrieval_depth": max(ks),
            "metric": "cosine",
            "relevance": "binary",
            "precision_denominator": "k",
            "mrr_cutoff": max(ks),
            "retrievals": list(retrievals),
            "search_path": "semantic: app.services.search.search (POST /api/v1/search); "
            "full_text/hybrid: app.services.search.hybrid_search (POST /api/v1/search/hybrid)",
            "hybrid_candidate_pool": base_settings.hybrid_candidate_pool,
            "hybrid_rrf_k": base_settings.hybrid_rrf_k,
            "query_scope": "documents of the query's language (document_ids filter)",
            "chunk_size_tokens": base_settings.chunk_size_tokens,
            "chunk_overlap_tokens": base_settings.chunk_overlap_tokens,
            "hf_hub_offline": offline,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "libraries": _library_versions(),
            **_database_versions(engine),
        },
        "models": results,
        "comparison": comparison_rows(results, ks),
    }


def render_markdown(report: dict[str, Any]) -> str:
    ks = report["settings"]["k"]
    header = ["Model", "Retrieval", "Scope"] + [f"R@{k}" for k in ks] + [f"P@{k}" for k in ks] + [
        f"MRR@{max(ks)}"
    ]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for model in report["models"]:
        if model["status"] != "ok":
            lines.append(
                f"| {model['model']} | {model.get('retrieval', 'semantic')} "
                f"| unavailable: {model['error'][:80]} |"
            )
            continue
        scopes = {"overall": model["metrics"]["overall"]} | model["metrics"]["by_language"] | {
            "macro (EN/RU/KK)": model["metrics"]["macro_language"]
        }
        for scope, m in scopes.items():
            cells = [f"{m[f'recall@{k}']:.3f}" for k in ks] + [
                f"{m[f'precision@{k}']:.3f}" for k in ks
            ] + [f"{m['mrr']:.3f}"]
            label = f"{model['model']} | {model.get('retrieval', 'semantic')}"
            lines.append(f"| {label} | {scope} | " + " | ".join(cells) + " |")
    lines.append("")
    for model in report["models"]:
        if model["status"] != "ok":
            continue
        misses = [q for q in model["queries"] if q["first_relevant_rank"] != 1]
        label = f"{model['model']} [{model.get('retrieval', 'semantic')}]"
        lines.append(f"{label}: {len(misses)} queries with no relevant passage at rank 1")
        for q in misses:
            top = q["retrieved"][0] if q["retrieved"] else None
            score = top.get("similarity", top.get("rrf_score")) if top else None
            got = f"{top['passage_id']} ({score})" if top else "nothing"
            lines.append(
                f"  {q['id']}: first relevant rank {q['first_relevant_rank']}, top-1 {got}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.evaluation.run_search_eval", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--models", default="current", help="comma list: current, e5-small, bge-m3")
    parser.add_argument("--k", default="1,3,5", help="cutoffs, e.g. 1,3,5 (max 20)")
    parser.add_argument(
        "--retrieval",
        default="semantic",
        help="comma list: semantic (POST /search), full_text, hybrid (POST /search/hybrid)",
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
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
        # Read by huggingface_hub at import time, which happens later, when a model loads.
        os.environ["HF_HUB_OFFLINE"] = "1"
    base_settings = Settings()
    try:
        models = resolve_models(args.models, base_settings.embedding_model)
        ks = parse_ks(args.k)
        retrievals = parse_retrievals(args.retrieval)
        url = os.environ.get(TEST_DB_ENV)
        if not url:
            raise EvaluationError(f"Set {TEST_DB_ENV} to a database whose name ends in '_test'")
        require_test_database(url)
    except (ValueError, EvaluationError) as exc:
        parser.error(str(exc))

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Per-query search logs would drown the summary; restored afterwards.
    search_logger = logging.getLogger("app.services.search")
    previous_level = search_logger.level
    search_logger.setLevel(logging.WARNING)
    dataset = load_dataset(args.dataset)
    engine = create_engine(url)
    try:
        report = run(
            engine, dataset, models, ks, base_settings, offline=args.offline, retrievals=retrievals
        )
    finally:
        engine.dispose()
        search_logger.setLevel(previous_level)

    # Semantic-only runs keep the Phase 5 file name; other runs never overwrite it.
    suffix = "" if retrievals == ["semantic"] else "_" + "-".join(retrievals)
    output = args.output or Path("docs/evaluation") / f"search_eval_v{dataset.version}{suffix}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(render_markdown(report))
    print(f"\nReport written to {output}")
    return 0 if any(m["status"] == "ok" for m in report["models"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
