"""PostgreSQL full-text retrieval: matching, multilingual handling, isolation and sync.

Passages come from the synthetic evaluation dataset. Each test runs in a rolled-back
transaction.
"""

import uuid

import numpy as np
import pytest
from sqlalchemy import delete, insert, text, update
from sqlalchemy.orm import Session

from app.evaluation.dataset import load_dataset
from app.models import EMBEDDING_DIMENSION, Document, DocumentChunk, User
from app.retrieval.full_text_search import full_text_search

pytestmark = pytest.mark.integration

MODEL = "test/model"
PASSAGES = load_dataset().passages()


def _user(session: Session, email: str = "fts@example.com") -> User:
    user = User(email=email)
    session.add(user)
    session.flush()
    return user


def _document(session: Session, user: User, passage_ids: list[str], model: str = MODEL) -> Document:
    document = Document(
        user_id=user.id,
        filename="doc.txt",
        content_type="text/plain",
        file_size=1,
        content_hash=uuid.uuid4().hex * 2,
    )
    session.add(document)
    session.flush()
    vector = np.ones(EMBEDDING_DIMENSION, dtype=np.float32) / np.sqrt(EMBEDDING_DIMENSION)
    session.execute(
        insert(DocumentChunk),
        [
            {
                "document_id": document.id,
                "chunk_index": i,
                "content": PASSAGES[pid].text,
                "char_start": 0,
                "char_end": len(PASSAGES[pid].text),
                "token_count": 1,
                "embedding": vector,
                "embedding_model": model,
            }
            for i, pid in enumerate(passage_ids)
        ],
    )
    return document


def _search(session: Session, user: User, query: str, **kwargs) -> list[str]:
    kwargs.setdefault("embedding_model", MODEL)
    kwargs.setdefault("limit", 10)
    hits = full_text_search(session, query, user_id=user.id, **kwargs)
    return [h.content for h in hits]


EN = ["en-billing-01", "en-billing-02", "en-billing-03", "en-delivery-01"]
RU = ["ru-payment-01", "ru-payment-02", "ru-contract-02", "ru-delivery-01"]
KK = ["kk-tuition-01", "kk-library-01", "kk-library-03", "kk-dorm-02"]


@pytest.fixture
def owner(db_session: Session) -> User:
    user = _user(db_session)
    _document(db_session, user, EN + RU + KK)
    return user


def text_of(pid: str) -> str:
    return PASSAGES[pid].text


def test_english_term_match_with_stemming(db_session: Session, owner: User) -> None:
    # "invoices" (query) and "Invoices"/"invoice" (passage) share the English stem.
    results = _search(db_session, owner, "invoices")
    assert results[0] == text_of("en-billing-01")
    assert text_of("en-billing-03") not in results


def test_russian_inflected_form_matches_through_stemming(db_session: Session, owner: User) -> None:
    # "счета" (query) vs "Счёт" / "счёта" (passages): Russian stemming plus ё/е normalization.
    assert set(_search(db_session, owner, "счета")) == {
        text_of("ru-payment-01"),
        text_of("ru-payment-02"),
    }


def test_kazakh_exact_word_form_matches(db_session: Session, owner: User) -> None:
    assert _search(db_session, owner, "кітапхана") == [text_of("kk-library-01")]


def test_kazakh_has_no_stemmer_so_other_inflections_may_miss(
    db_session: Session, owner: User
) -> None:
    # Documented limitation: "кітапханаға" (dative) is not reduced to "кітапхана".
    assert _search(db_session, owner, "кітапханаға") == []


def test_any_term_matching_ranks_more_matching_terms_higher(
    db_session: Session, owner: User
) -> None:
    results = _search(db_session, owner, "late fee for payment of the invoice")
    assert results[0] == text_of("en-billing-02")  # matches late, fee, payment
    assert text_of("en-billing-01") in results  # matches only "invoice"/"paid"


def test_no_match_returns_empty(db_session: Session, owner: User) -> None:
    assert _search(db_session, owner, "zebra giraffe") == []


@pytest.mark.parametrize("query", ["", "   ", "?!.,;:", "--- &&& ||| !!!", "« » — …"])
def test_queries_without_usable_terms_return_empty_not_an_error(
    db_session: Session, owner: User, query: str
) -> None:
    assert _search(db_session, owner, query) == []


def test_stop_words_still_match_through_the_simple_channel(
    db_session: Session, owner: User
) -> None:
    # Documented trade-off: "simple" keeps stop words (no Kazakh stop list exists, and an
    # English/Russian stop list measured no gain), so "the" alone matches broadly.
    assert len(_search(db_session, owner, "the")) > 1


@pytest.mark.parametrize("query", ["x'); DROP TABLE users; --", "a & | ! <-> :* b", "'''"])
def test_operator_and_quote_characters_are_plain_text(
    db_session: Session, owner: User, query: str
) -> None:
    _search(db_session, owner, query)  # no syntax error
    assert db_session.scalar(text("SELECT count(*) FROM users")) >= 1


def test_document_filter(db_session: Session, owner: User) -> None:
    other = _document(db_session, owner, ["en-billing-04"])
    results = _search(db_session, owner, "invoices", document_ids=[other.id])
    assert results == [text_of("en-billing-04")]


def test_other_users_chunks_are_never_returned(db_session: Session, owner: User) -> None:
    stranger = _user(db_session, "stranger@example.com")
    foreign = _document(db_session, stranger, ["en-billing-04"])
    assert text_of("en-billing-04") not in _search(db_session, owner, "refunds overpaid")
    assert _search(db_session, owner, "refunds", document_ids=[foreign.id]) == []
    assert _search(db_session, stranger, "refunds") == [text_of("en-billing-04")]


def test_chunks_of_another_embedding_model_are_not_eligible(db_session: Session) -> None:
    user = _user(db_session)
    _document(db_session, user, ["en-leave-01"], model="old/model")
    assert _search(db_session, user, "vacation leave") == []
    assert _search(db_session, user, "leave", embedding_model="old/model") == [
        text_of("en-leave-01")
    ]


def test_search_vector_follows_content_updates_and_reindexing(
    db_session: Session, owner: User
) -> None:
    [chunk_id] = db_session.scalars(
        text("SELECT id FROM document_chunks WHERE content = :c"), {"c": text_of("en-billing-03")}
    ).all()
    db_session.execute(
        update(DocumentChunk)
        .where(DocumentChunk.id == chunk_id)
        .values(content="Payments by cryptocurrency are accepted.", char_end=40)
    )
    assert _search(db_session, owner, "cryptocurrency") == [
        "Payments by cryptocurrency are accepted."
    ]
    assert text_of("en-billing-03") not in _search(db_session, owner, "cash")

    # Re-index: chunks deleted and inserted again (what the worker does).
    document_id = db_session.scalar(
        text("SELECT document_id FROM document_chunks WHERE id = :i"), {"i": chunk_id}
    )
    db_session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
    assert _search(db_session, owner, "cryptocurrency") == []
    db_session.execute(delete(Document).where(Document.id == document_id))
    _document(db_session, owner, ["en-billing-03"])
    assert _search(db_session, owner, "cash") == [text_of("en-billing-03")]


def test_gin_index_exists_and_can_serve_the_query(db_session: Session, owner: User) -> None:
    indexdef = db_session.scalar(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_document_chunks_search_vector'")
    )
    assert "USING gin (search_vector)" in indexdef
    # Tiny tables are seq-scanned; disable that to prove the index is usable for @@.
    db_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        db_session.scalars(
            text(
                "EXPLAIN SELECT id FROM document_chunks WHERE search_vector @@ "
                "plainto_tsquery('simple'::regconfig, :q)"
            ),
            {"q": "invoice"},
        )
    )
    assert "ix_document_chunks_search_vector" in plan


def test_search_vector_is_a_generated_column(db_session: Session) -> None:
    generated = db_session.scalar(
        text(
            "SELECT is_generated FROM information_schema.columns "
            "WHERE table_name = 'document_chunks' AND column_name = 'search_vector'"
        )
    )
    assert generated == "ALWAYS"
