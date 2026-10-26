"""Grounding policy quote-match-v1: output contract, citation identity, quotes, numbers."""

import json
import uuid

import pytest

from app.rag.context import ContextSource, GenerationContext
from app.rag.grounding import (
    Abstention,
    AbstentionReason,
    GroundedAnswer,
    evaluate_reply,
    locate_quote,
    parse_reply,
)

DOC_A, DOC_B = uuid.uuid4(), uuid.uuid4()


def _source(n: int, content: str, document_id=DOC_A, page: int | None = 1, start: int = 0):
    return ContextSource(
        source_id=f"S{n}",
        document_id=document_id,
        filename="a.pdf" if document_id == DOC_A else "b.txt",
        chunk_id=uuid.uuid4(),
        chunk_index=n - 1,
        page_number=page,
        char_start=start,
        char_end=start + len(content),
        content=content,
        tokens=10,
    )


CONTEXT = GenerationContext(
    sources=(
        _source(1, "Invoices must be paid within 30 days of the invoice date.", page=2, start=100),
        _source(2, "A late fee of 1.5% applies per month of delay.", page=3),
        _source(3, "Счёт оплачивается в течение  30 дней.", document_id=DOC_B, page=None),
    ),
    token_budget=1000,
    token_counter="test",
    excluded_empty=0,
    excluded_over_budget=0,
)


def reply(answer: str = "", *citations: tuple[str, str], status: str = "answered") -> str:
    return json.dumps(
        {
            "status": status,
            "answer": answer,
            "citations": [{"source_id": s, "quote": q} for s, q in citations],
        },
        ensure_ascii=False,
    )


def _abstains(raw: str, reason: AbstentionReason) -> None:
    assert evaluate_reply(raw, CONTEXT) == Abstention(reason)


def test_valid_answer_returns_stored_quote_and_page_offsets() -> None:
    result = evaluate_reply(
        reply("Within 30 days [S1].", ("S1", "paid within 30 days")), CONTEXT
    )

    assert isinstance(result, GroundedAnswer)
    assert result.answer == "Within 30 days [S1]."
    [citation] = result.citations
    assert citation.source.source_id == "S1"
    [quote] = citation.quotes
    assert quote.text == "paid within 30 days"
    # Offsets are relative to the page text: chunk start 100 + position in the chunk.
    assert (quote.char_start, quote.char_end) == (100 + 17, 100 + 36)


def test_multiple_sources_and_documents_keep_first_cited_order() -> None:
    result = evaluate_reply(
        reply(
            "30 days, then 1.5% per month.",
            ("S2", "late fee of 1.5%"),
            ("S1", "within 30 days"),
            ("S3", "в течение 30 дней"),
        ),
        CONTEXT,
    )
    assert isinstance(result, GroundedAnswer)
    assert [c.source.source_id for c in result.citations] == ["S2", "S1", "S3"]
    assert {c.source.document_id for c in result.citations} == {DOC_A, DOC_B}
    assert result.citations[2].source.page_number is None


def test_repeated_citations_are_deduplicated() -> None:
    result = evaluate_reply(
        reply(
            "Within 30 days.",
            ("S1", "within 30 days"),
            ("S1", "within 30 days"),
            ("S1", "the invoice date"),
            ("S1", "WITHIN 30 DAYS"),
        ),
        CONTEXT,
    )
    assert isinstance(result, GroundedAnswer)
    [citation] = result.citations
    assert [q.text for q in citation.quotes] == ["within 30 days", "the invoice date"]


def test_quote_match_tolerates_case_whitespace_and_wrappers() -> None:
    passage = "Счёт оплачивается в течение  30 дней."
    start = passage.index("в течение")
    # The passage has two spaces before "30"; the span covers the stored text exactly.
    assert locate_quote("«в течение 30 дней»", passage) == (start, passage.index("."))
    assert locate_quote("...СЧЁТ оплачивается…", passage) == (0, len("Счёт оплачивается"))
    assert locate_quote("в течение 31 дня", passage) is None


@pytest.mark.parametrize("quote", ["30", "  of  ", "", "..."])
def test_too_short_quotes_prove_nothing(quote: str) -> None:
    _abstains(reply("30 days.", ("S1", quote)), AbstentionReason.UNVERIFIED_QUOTE)


def test_hallucinated_source_id() -> None:
    _abstains(reply("30 days.", ("S7", "within 30 days")), AbstentionReason.INVALID_CITATION)


def test_database_id_instead_of_source_id_is_invalid() -> None:
    # The model never sees chunk ids, but a guessed or injected one must not pass either.
    chunk_id = str(CONTEXT.sources[0].chunk_id)
    _abstains(reply("30 days.", (chunk_id, "within 30 days")), AbstentionReason.INVALID_CITATION)


def test_one_invalid_citation_rejects_the_whole_answer() -> None:
    _abstains(
        reply("30 days.", ("S1", "within 30 days"), ("S4", "anything")),
        AbstentionReason.INVALID_CITATION,
    )


def test_marker_for_a_source_outside_the_context() -> None:
    _abstains(
        reply("30 days [S1][S5].", ("S1", "within 30 days")), AbstentionReason.INVALID_CITATION
    )


def test_quote_from_another_source_is_unverified() -> None:
    # Real text, wrong source: S2 does not contain the S1 sentence.
    _abstains(reply("30 days.", ("S2", "within 30 days")), AbstentionReason.UNVERIFIED_QUOTE)


def test_fabricated_quote_is_unverified() -> None:
    _abstains(
        reply("60 days.", ("S1", "must be paid within 60 days")), AbstentionReason.UNVERIFIED_QUOTE
    )


def test_invented_number_with_real_quote_is_unsupported() -> None:
    _abstains(
        reply("Within 45 days.", ("S1", "within 30 days")), AbstentionReason.UNSUPPORTED_NUMBER
    )


def test_numbers_from_any_cited_passage_and_page_are_allowed() -> None:
    result = evaluate_reply(
        reply("Page 2: 30 days; 1,5% late fee.", ("S1", "within 30 days"), ("S2", "late fee")),
        CONTEXT,
    )
    assert isinstance(result, GroundedAnswer)


def test_number_from_an_uncited_passage_is_unsupported() -> None:
    _abstains(
        reply("30 days and 1.5% fee.", ("S1", "within 30 days")),
        AbstentionReason.UNSUPPORTED_NUMBER,
    )


def test_model_abstention_wins_even_with_citations() -> None:
    _abstains(
        reply("", ("S1", "within 30 days"), status="insufficient_evidence"),
        AbstentionReason.MODEL_INSUFFICIENT_EVIDENCE,
    )


def test_answer_without_citations() -> None:
    _abstains(reply("30 days."), AbstentionReason.MISSING_CITATIONS)


@pytest.mark.parametrize("raw", ["", "   \n", "\t"])
def test_empty_output(raw: str) -> None:
    _abstains(raw, AbstentionReason.EMPTY_MODEL_OUTPUT)


@pytest.mark.parametrize(
    "raw",
    [
        "The answer is 30 days.",
        "[1, 2]",
        '"answered"',
        '{"status": "maybe", "answer": "x", "citations": []}',
        '{"answer": "30 days", "citations": []}',
        '{"status": "answered", "answer": 30, "citations": []}',
        '{"status": "answered", "answer": "x", "citations": [{"source_id": 1, "quote": "q"}]}',
        '{"status": "answered", "answer": "x", "citations": "S1"}',
        '{"status": "answered", "answer": "   ", "citations": [{"source_id": "S1", "quote": "x"}]}',
        '{"status": "answered", "answer": "x" ',
        json.dumps({"status": "answered", "answer": "x" * 4001, "citations": []}),
    ],
)
def test_malformed_output(raw: str) -> None:
    _abstains(raw, AbstentionReason.MALFORMED_MODEL_OUTPUT)


def test_code_fence_and_extra_keys_are_tolerated() -> None:
    raw = "```json\n" + json.dumps(
        {
            "status": "answered",
            "answer": "Within 30 days.",
            "citations": [{"source_id": "S1", "quote": "within 30 days", "note": "x"}],
            "reasoning": "ignored",
        }
    ) + "\n```"
    assert isinstance(evaluate_reply(raw, CONTEXT), GroundedAnswer)


def test_parse_reply_returns_model_fields() -> None:
    parsed = parse_reply(reply("a", ("S1", "q")))
    assert not isinstance(parsed, Abstention)
    assert (parsed.status, parsed.citations[0].source_id) == ("answered", "S1")


def test_empty_context_can_never_produce_an_answer() -> None:
    empty = GenerationContext((), 100, "test", 0, 0)
    assert evaluate_reply(reply("30 days.", ("S1", "within 30 days")), empty) == Abstention(
        AbstentionReason.INVALID_CITATION
    )
