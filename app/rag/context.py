"""Deterministic, bounded generation context built from retrieved chunks.

Input: the hits of the production retrieval (already restricted to the caller's documents,
the document filter and the active embedding model), in rank order. Output: the passages
the model will see, each labelled with a source id S1, S2, ... in that order, together with
the chunk's database identity and provenance.

Rules:
- Whole chunks only. A chunk is never cut to fit the budget: a cut passage would no longer
  match its stored offsets (content == page_text[char_start:char_end]) and could lose the
  sentence that carries the evidence. A chunk that does not fit is skipped and the next,
  smaller one may still fit, so the result is the same for the same hits every time.
- Empty (whitespace-only) chunks are skipped: they cannot support anything.
- Source ids are assigned after filtering, so they are dense (S1..Sn) and exist only for
  passages the model actually receives. Anything else the model cites is invalid.
- The budget counts each passage as serialized for the prompt (JSON-escaped, with its id,
  filename and page), so escaping overhead is included.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from app.rag.tokens import TokenCounter


class RetrievedChunk(Protocol):
    """What the context needs from a hit; vector, full-text and fused hits all provide it."""

    document_id: uuid.UUID
    filename: str
    chunk_id: uuid.UUID
    chunk_index: int
    page_number: int | None
    char_start: int
    char_end: int
    content: str


@dataclass(frozen=True)
class ContextSource:
    source_id: str
    document_id: uuid.UUID
    filename: str
    chunk_id: uuid.UUID
    chunk_index: int
    page_number: int | None
    char_start: int
    char_end: int
    content: str
    tokens: int

    def prompt_entry(self) -> dict[str, object]:
        # Only what the model needs. Database ids stay server-side: the model refers to
        # passages by source id alone, so it has no way to name any other chunk.
        return {
            "id": self.source_id,
            "filename": self.filename,
            "page": self.page_number,
            "text": self.content,
        }


@dataclass(frozen=True)
class GenerationContext:
    sources: tuple[ContextSource, ...]
    token_budget: int
    token_counter: str
    excluded_empty: int
    excluded_over_budget: int

    @property
    def tokens(self) -> int:
        return sum(source.tokens for source in self.sources)

    def by_id(self) -> dict[str, ContextSource]:
        return {source.source_id: source for source in self.sources}


def serialize_entry(entry: dict[str, object]) -> str:
    return json.dumps(entry, ensure_ascii=False)


def build_context(
    hits: Sequence[RetrievedChunk], counter: TokenCounter, token_budget: int
) -> GenerationContext:
    if token_budget < 1:
        raise ValueError("token_budget must be >= 1")
    sources: list[ContextSource] = []
    seen: set[uuid.UUID] = set()
    used = empty = over_budget = 0
    for hit in hits:
        if hit.chunk_id in seen:
            continue
        seen.add(hit.chunk_id)
        if not hit.content.strip():
            empty += 1
            continue
        source = ContextSource(
            source_id=f"S{len(sources) + 1}",
            document_id=hit.document_id,
            filename=hit.filename,
            chunk_id=hit.chunk_id,
            chunk_index=hit.chunk_index,
            page_number=hit.page_number,
            char_start=hit.char_start,
            char_end=hit.char_end,
            content=hit.content,
            tokens=0,
        )
        tokens = counter.count(serialize_entry(source.prompt_entry()))
        if used + tokens > token_budget:
            over_budget += 1
            continue
        used += tokens
        sources.append(replace(source, tokens=tokens))
    return GenerationContext(
        sources=tuple(sources),
        token_budget=token_budget,
        token_counter=counter.name,
        excluded_empty=empty,
        excluded_over_budget=over_budget,
    )
