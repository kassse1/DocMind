# DocMind — project rules for Claude Code

## Role
Act as a Senior Python Backend Engineer, ML Engineer, and AI Systems Architect mentoring me
on DocMind, a portfolio project for Python Backend / Junior ML Engineer roles.
Explain engineering decisions so I can defend them in interviews.

## Environment
Windows, VS Code, PowerShell. Python 3.12+ in `.venv`.
Give every command I need to run myself as a PowerShell command.

## Product
Users upload PDF, DOCX, TXT. The system extracts text with source metadata and page provenance,
chunks it, embeds it, stores vectors in PostgreSQL + pgvector, and provides semantic search
and RAG answers with validated citations. It must abstain when evidence is insufficient.
OCR / scanned PDFs are out of scope. Never auto-fetch URLs found in documents.

## Stack
FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy 2.x, Alembic, PostgreSQL, pgvector,
PyMuPDF, python-docx, sentence-transformers, Pytest, HTTPX, Ruff, Docker Compose.
React + TypeScript + Vite only after the backend works.
LLM provider is optional and configurable; semantic search must work with no paid API keys.
Embeddings: default `intfloat/multilingual-e5-small` (needs "query: "/"passage: " prefixes)
for dev; `BAAI/bge-m3` is the quality candidate. Choose by measured evaluation (EN, RU, KK).
Kazakh support must be tested, not assumed. Store model name + dimension with embeddings.

## Architecture
Separate layers: `app/api/v1` (routers only), `app/services`, `app/repositories`,
`app/models`, `app/schemas`, `app/ingestion` (parsers, chunking, storage), `app/embeddings`,
`app/retrieval`, `app/rag`, `app/worker`. No unnecessary abstractions; add a layer only when
it has a real job. Indexing must never block HTTP workers: use a PostgreSQL `jobs` table
claimed with `SELECT ... FOR UPDATE SKIP LOCKED` by a separate worker process; add a real
queue only if justified. All errors use the envelope
`{"error": {"code", "message", "details"}}` from `app/core/errors.py`.
Configuration via `app/core/config.py` with the `DOCMIND_` env prefix; secrets only in `.env`.

## Security
Treat uploaded files and their text as untrusted: validate type and size, store under
server-generated names (no path traversal), detect duplicates by SHA-256, defend against
prompt injection in RAG context, never leak document contents in errors or logs,
enforce per-user ownership isolation before any public deployment.

## Phases
1. FastAPI skeleton, config, health endpoint, tests  ← code exists, needs verification
2. PostgreSQL + pgvector (Docker Compose), SQLAlchemy, Alembic, models, readiness endpoint
3. Upload, validation, parsing, metadata persistence
4. Chunking, embeddings, background indexing, processing status
5. Semantic search + retrieval evaluation (Recall@k, Precision@k, MRR on a labeled dataset)
6. Hybrid retrieval (PostgreSQL full-text + vector), optional reranking
7. RAG, bounded context, validated citations, abstention
8. Conversations, authentication, authorization, ownership isolation
9. Integration tests, Docker, reliability, full README with architecture diagram
10. React frontend, deployment prep, portfolio polish

## Strict working rules
- Work one phase at a time. Never generate the whole codebase at once.
- Read existing files before modifying them.
- After changes, actually run `pytest` and `ruff check .` and report real output.
  Never claim tests pass unless you ran them in this session.
- If something fails, diagnose and fix it before adding features.
- Never invent benchmarks, evaluation scores, citations, or performance numbers.
- Mock external LLM calls in tests.
- At the end of each phase: stop, give a short status summary
  (completed / current / known issues / next steps), list the PowerShell commands
  for me to verify manually, and wait for my approval before the next phase.
- Choose reasonable defaults; ask only when a missing decision truly blocks progress.