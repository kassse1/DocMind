"""Parse the model's reply and decide whether it may be shown as a grounded answer.

Policy "quote-match-v1". The reply must be one JSON object:
    {"status": "answered" | "insufficient_evidence", "answer": str,
     "citations": [{"source_id": "S1", "quote": str}, ...]}
An answer is accepted only if ALL of these hold; otherwise the request abstains with a
machine-readable reason and no answer text is returned:
1. Contract: valid JSON of that shape, a non-empty answer, at least one citation.
2. Citation identity: every source_id (and every [Sn] marker in the answer) names a
   passage that was in this request's context. The context holds only the caller's chunks,
   so a foreign, invented or excluded chunk can never be cited.
3. Quote verification: every citation carries a quote of at least MIN_QUOTE_CHARS that
   occurs in its cited passage (exact, or ignoring case and whitespace differences). The
   returned quote and its offsets are cut from the stored text, never copied from the model.
4. Numbers: every number in the answer occurs in the text (or is the page number) of a
   cited passage. Invented
   amounts, dates and percentages are a common failure, and this check is language-neutral.
What this does NOT establish: that the answer follows from the quotes (entailment). A model
can quote a real sentence and still draw a wrong conclusion from it. Responses therefore
report entailment_verified = false; a semantic checker (NLI model or second LLM pass)
would be a separate, measured addition.
"""

import enum
import json
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.rag.context import ContextSource, GenerationContext

GROUNDING_POLICY = "quote-match-v1"
MAX_ANSWER_CHARS = 4000
MAX_CITATIONS = 20
MAX_QUOTE_CHARS = 1000
# Shorter quotes ("30", "the") occur almost anywhere and prove nothing.
MIN_QUOTE_CHARS = 5


class AbstentionReason(enum.StrEnum):
    NO_RETRIEVED_CHUNKS = "no_retrieved_chunks"
    NO_ELIGIBLE_CONTEXT = "no_eligible_context"
    MODEL_INSUFFICIENT_EVIDENCE = "model_insufficient_evidence"
    EMPTY_MODEL_OUTPUT = "empty_model_output"
    MALFORMED_MODEL_OUTPUT = "malformed_model_output"
    MISSING_CITATIONS = "missing_citations"
    INVALID_CITATION = "invalid_citation"
    UNVERIFIED_QUOTE = "unverified_quote"
    UNSUPPORTED_NUMBER = "unsupported_number"


class _ModelCitation(BaseModel):
    # strict: "source_id": 1 is a contract violation, not something to coerce.
    model_config = ConfigDict(extra="ignore", strict=True)

    source_id: str = Field(max_length=64)
    quote: str = Field(max_length=MAX_QUOTE_CHARS)


class _ModelReply(BaseModel):
    # Extra keys (e.g. a "reasoning" field) are ignored: they are never shown and do not
    # change what is checked.
    model_config = ConfigDict(extra="ignore", strict=True)

    status: Literal["answered", "insufficient_evidence"]
    answer: str = Field(default="", max_length=MAX_ANSWER_CHARS)
    citations: list[_ModelCitation] = Field(default_factory=list, max_length=MAX_CITATIONS)


@dataclass(frozen=True)
class VerifiedQuote:
    # Cut from the stored passage; offsets are in the same coordinates as the chunk's
    # char_start/char_end (normalized page text, or document text without pages).
    text: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class VerifiedCitation:
    source: ContextSource
    quotes: tuple[VerifiedQuote, ...]


@dataclass(frozen=True)
class GroundedAnswer:
    answer: str
    citations: tuple[VerifiedCitation, ...]


@dataclass(frozen=True)
class Abstention:
    reason: AbstentionReason


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
# Only bracketed markers ("[S2]"): a bare "S3" may be part of a name in the answer.
_SOURCE_MARKER = re.compile(r"\[S(\d+)\]")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# Quotation marks and ellipses a model tends to wrap around a quote.
_QUOTE_WRAPPERS = "\"'`«»“”„‘’ \t\r\n"


def parse_reply(raw: str) -> _ModelReply | Abstention:
    text = raw.strip()
    if not text:
        return Abstention(AbstentionReason.EMPTY_MODEL_OUTPUT)
    # A single ```json fence around the object is a common, harmless deviation.
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return _ModelReply.model_validate(data)
    except (ValueError, ValidationError):
        return Abstention(AbstentionReason.MALFORMED_MODEL_OUTPUT)


def _clean_quote(quote: str) -> str:
    quote = quote.strip(_QUOTE_WRAPPERS)
    for ellipsis in ("...", "…"):
        quote = quote.removeprefix(ellipsis).removesuffix(ellipsis)
    return quote.strip(_QUOTE_WRAPPERS)


def locate_quote(quote: str, passage: str) -> tuple[int, int] | None:
    """Span of the quote in the passage: exact first, then ignoring case and runs of
    whitespace (PDF line breaks, double spaces). Returns None if it is not there."""
    quote = _clean_quote(quote)
    if len("".join(quote.split())) < MIN_QUOTE_CHARS:
        return None
    start = passage.find(quote)
    if start >= 0:
        return start, start + len(quote)
    pattern = r"\s+".join(re.escape(word) for word in quote.split())
    match = re.search(pattern, passage, re.IGNORECASE)
    return match.span() if match else None


def _numbers(text: str) -> set[str]:
    # Separators dropped, so "1,5" matches "1.5" and "10 000"-style groups stay comparable
    # digit by digit. Lenient on purpose: it can miss "15" vs "1.5", never flag a real match.
    return {re.sub(r"[.,]", "", number) for number in _NUMBER.findall(text)}


def evaluate_reply(raw: str, context: GenerationContext) -> GroundedAnswer | Abstention:
    reply = parse_reply(raw)
    if isinstance(reply, Abstention):
        return reply
    if reply.status == "insufficient_evidence":
        return Abstention(AbstentionReason.MODEL_INSUFFICIENT_EVIDENCE)
    answer = reply.answer.strip()
    if not answer:
        return Abstention(AbstentionReason.MALFORMED_MODEL_OUTPUT)
    if not reply.citations:
        return Abstention(AbstentionReason.MISSING_CITATIONS)

    sources = context.by_id()
    markers = {f"S{n}" for n in _SOURCE_MARKER.findall(answer)}
    if any(marker not in sources for marker in markers):
        return Abstention(AbstentionReason.INVALID_CITATION)

    # Identity first for every citation, so a fabricated id is reported as such even when
    # another citation also has a bad quote.
    if any(c.source_id.strip() not in sources for c in reply.citations):
        return Abstention(AbstentionReason.INVALID_CITATION)

    quotes: dict[str, dict[tuple[int, int], VerifiedQuote]] = {}
    for citation in reply.citations:
        source = sources[citation.source_id.strip()]
        span = locate_quote(citation.quote, source.content)
        if span is None:
            return Abstention(AbstentionReason.UNVERIFIED_QUOTE)
        start, end = span
        # Dict keyed by span: the same quote cited twice is kept once, first-seen order.
        quotes.setdefault(source.source_id, {})[span] = VerifiedQuote(
            text=source.content[start:end],
            char_start=source.char_start + start,
            char_end=source.char_start + end,
        )

    # Page numbers are evidence the model was given too ("see page 4").
    cited = [sources[source_id] for source_id in quotes]
    allowed = _numbers(" ".join(s.content for s in cited)) | {
        str(s.page_number) for s in cited if s.page_number is not None
    }
    if not _numbers(_SOURCE_MARKER.sub(" ", answer)) <= allowed:
        return Abstention(AbstentionReason.UNSUPPORTED_NUMBER)

    return GroundedAnswer(
        answer=answer,
        citations=tuple(
            VerifiedCitation(source=sources[source_id], quotes=tuple(spans.values()))
            for source_id, spans in quotes.items()
        ),
    )
