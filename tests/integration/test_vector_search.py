"""Vector ranking against real PostgreSQL + pgvector, with hand-built embeddings.

Fake model vectors carry no meaning, so ranking is tested with vectors whose cosine
similarity to the query is known exactly. Each test runs in a rolled-back transaction.
"""

import uuid

import numpy as np
import pytest
from sqlalchemy.orm import Session

from app.models import EMBEDDING_DIMENSION, Document, DocumentChunk, User
from app.retrieval.vector_search import vector_search

pytestmark = pytest.mark.integration

MODEL = "test/axes"


def vec(*weights: float) -> np.ndarray:
    """Unit vector with the given leading components (the rest are zero)."""
    v = np.zeros(EMBEDDING_DIMENSION, dtype=np.float32)
    v[: len(weights)] = weights
    return v / np.linalg.norm(v)


QUERY = vec(1.0)


def _user(session: Session, email: str = "search@example.com") -> User:
    user = User(email=email)
    session.add(user)
    session.flush()
    return user


def _document(session: Session, user: User, filename: str = "doc.txt") -> Document:
    document = Document(
        user_id=user.id,
        filename=filename,
        content_type="text/plain",
        file_size=1,
        content_hash=uuid.uuid4().hex * 2,
    )
    session.add(document)
    session.flush()
    return document


def _chunk(
    session: Session,
    document: Document,
    index: int,
    embedding: np.ndarray,
    content: str | None = None,
    model: str = MODEL,
) -> DocumentChunk:
    content = content or f"chunk {index}"
    chunk = DocumentChunk(
        document_id=document.id,
        chunk_index=index,
        content=content,
        page_number=None,
        char_start=0,
        char_end=len(content),
        token_count=2,
        embedding=embedding,
        embedding_model=model,
    )
    session.add(chunk)
    session.flush()
    return chunk


def _search(session: Session, user: User, **kwargs) -> list:
    kwargs.setdefault("top_k", 10)
    kwargs.setdefault("embedding_model", MODEL)
    return vector_search(session, kwargs.pop("query", QUERY), user_id=user.id, **kwargs)


def test_more_similar_chunk_ranks_first_with_cosine_scores(db_session: Session) -> None:
    user = _user(db_session)
    doc = _document(db_session, user, "contract.pdf")
    _chunk(db_session, doc, 0, vec(0.0, 1.0), "unrelated")           # similarity 0
    _chunk(db_session, doc, 1, vec(1.0), "exact match")               # 1
    _chunk(db_session, doc, 2, vec(1.0, 1.0), "related")              # 1/sqrt(2)
    _chunk(db_session, doc, 3, vec(-1.0), "opposite")                 # -1

    hits = _search(db_session, user)

    assert [h.content for h in hits] == ["exact match", "related", "unrelated", "opposite"]
    assert [h.similarity for h in hits] == pytest.approx(
        [1.0, 1 / np.sqrt(2), 0.0, -1.0], abs=1e-5
    )
    top = hits[0]
    assert (top.document_id, top.filename, top.chunk_index) == (doc.id, "contract.pdf", 1)
    assert (top.char_start, top.char_end, top.page_number) == (0, len("exact match"), None)


def test_top_k_limits_results(db_session: Session) -> None:
    user = _user(db_session)
    doc = _document(db_session, user)
    for i in range(5):
        _chunk(db_session, doc, i, vec(1.0, float(i)))
    hits = _search(db_session, user, top_k=2)
    assert [h.chunk_index for h in hits] == [0, 1]


def test_equal_scores_break_ties_by_document_then_chunk_index(db_session: Session) -> None:
    user = _user(db_session)
    docs = sorted((_document(db_session, user) for _ in range(2)), key=lambda d: d.id)
    # Inserted in scrambled order, all with the same vector.
    for doc, index in [(docs[1], 1), (docs[0], 2), (docs[1], 0), (docs[0], 0)]:
        _chunk(db_session, doc, index, vec(1.0, 1.0))

    expected = [(docs[0].id, 0), (docs[0].id, 2), (docs[1].id, 0), (docs[1].id, 1)]
    for _ in range(3):
        hits = _search(db_session, user)
        assert [(h.document_id, h.chunk_index) for h in hits] == expected


def test_document_filter(db_session: Session) -> None:
    user = _user(db_session)
    wanted, other = _document(db_session, user), _document(db_session, user)
    _chunk(db_session, other, 0, vec(1.0))
    _chunk(db_session, wanted, 0, vec(1.0, 1.0))

    hits = _search(db_session, user, document_ids=[wanted.id])

    assert [h.document_id for h in hits] == [wanted.id]


def test_min_similarity_threshold(db_session: Session) -> None:
    user = _user(db_session)
    doc = _document(db_session, user)
    _chunk(db_session, doc, 0, vec(1.0))           # 1.0
    _chunk(db_session, doc, 1, vec(1.0, 1.0))      # 0.707
    _chunk(db_session, doc, 2, vec(0.0, 1.0))      # 0.0

    assert [h.chunk_index for h in _search(db_session, user, min_similarity=0.7)] == [0, 1]
    assert [h.chunk_index for h in _search(db_session, user, min_similarity=0.0)] == [0, 1, 2]
    assert _search(db_session, user, query=vec(0.0, 0.0, 1.0), min_similarity=0.5) == []


def test_other_users_chunks_are_never_returned(db_session: Session) -> None:
    alice, bob = _user(db_session, "alice@example.com"), _user(db_session, "bob@example.com")
    _chunk(db_session, _document(db_session, bob), 0, vec(1.0), "bob's secret")
    _chunk(db_session, _document(db_session, alice), 0, vec(0.0, 1.0), "alice's note")

    assert [h.content for h in _search(db_session, alice)] == ["alice's note"]
    # Even when Alice names Bob's document explicitly (the service also rejects this).
    bob_doc = _search(db_session, bob)[0].document_id
    assert _search(db_session, alice, document_ids=[bob_doc]) == []


def test_chunks_of_another_model_are_ignored(db_session: Session) -> None:
    user = _user(db_session)
    doc = _document(db_session, user)
    _chunk(db_session, doc, 0, vec(1.0), "old model", model="other/model")
    _chunk(db_session, doc, 1, vec(0.0, 1.0), "current model")

    assert [h.content for h in _search(db_session, user)] == ["current model"]


def test_no_indexed_chunks_returns_empty(db_session: Session) -> None:
    user = _user(db_session)
    assert _search(db_session, user) == []
    _document(db_session, user)  # uploaded but not indexed yet
    assert _search(db_session, user) == []
