import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session

from app.models import (
    EMBEDDING_DIMENSION,
    Conversation,
    Document,
    DocumentChunk,
    DocumentStatus,
    Job,
    JobKind,
    JobStatus,
    Message,
    MessageRole,
    User,
)
from tests.integration.conftest import expect_violation

pytestmark = pytest.mark.integration


def _sha256(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _user(session: Session, email: str = "alice@example.com") -> User:
    user = User(email=email)
    session.add(user)
    session.flush()
    return user


def _document(session: Session, user: User, seed: str = "file-1") -> Document:
    doc = Document(
        user=user,
        filename=f"{seed}.pdf",
        content_type="application/pdf",
        file_size=1024,
        content_hash=_sha256(seed),
    )
    session.add(doc)
    session.flush()
    return doc


def _unit_vector(axis: int = 0) -> np.ndarray:
    vector = np.zeros(EMBEDDING_DIMENSION, dtype=np.float32)
    vector[axis] = 1.0
    return vector


def _chunk(content: str = "c", **kwargs: object) -> DocumentChunk:
    """A chunk with valid values for every required column; override any via kwargs."""
    values: dict[str, object] = {
        "chunk_index": 0,
        "content": content,
        "char_start": 0,
        "char_end": max(len(content), 1),
        "token_count": 1,
        "embedding": _unit_vector(),
        "embedding_model": "test-model",
    }
    values.update(kwargs)
    return DocumentChunk(content=values.pop("content"), **values)


def _count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_relationships_round_trip(db_session: Session) -> None:
    user = _user(db_session)
    doc = _document(db_session, user)
    # Added out of order to prove relationship ordering by chunk_index.
    doc.chunks.append(_chunk("second", chunk_index=1, page_number=2))
    doc.chunks.append(
        _chunk("Қазақстан", chunk_index=0, page_number=1, chunk_metadata={"k": "қ"})
    )
    conv = Conversation(user=user, title="Q&A")
    conv.messages.append(Message(role=MessageRole.USER, content="question"))
    db_session.add(conv)
    db_session.flush()
    db_session.expire_all()

    loaded = db_session.get(User, user.id)
    assert loaded is not None
    [loaded_doc] = loaded.documents
    assert loaded_doc.status is DocumentStatus.PENDING
    assert loaded_doc.error_message is None
    assert [c.chunk_index for c in loaded_doc.chunks] == [0, 1]
    assert loaded_doc.chunks[0].content == "Қазақстан"
    assert loaded_doc.chunks[0].chunk_metadata == {"k": "қ"}
    assert loaded_doc.chunks[1].chunk_metadata == {}
    assert loaded.conversations[0].messages[0].role is MessageRole.USER
    assert loaded.conversations[0].messages[0].citations is None


def test_timestamps_are_timezone_aware_utc(db_session: Session) -> None:
    user = _user(db_session)
    db_session.refresh(user)

    # timestamptz comes back aware; comparing with an aware UTC "now" would raise if it weren't.
    assert user.created_at.tzinfo is not None
    assert abs(user.created_at - datetime.now(UTC)) < timedelta(minutes=5)
    # Both default to now(), which is the transaction start time in PostgreSQL.
    assert user.updated_at == user.created_at


def test_email_unique_including_case_variants(db_session: Session) -> None:
    _user(db_session, "Alice@Example.com")

    db_session.add(User(email="  alice@example.COM "))
    expect_violation(db_session, "uq_users_email", db_session.flush)


def test_duplicate_chunk_index_rejected_within_document(db_session: Session) -> None:
    user = _user(db_session)
    doc_a = _document(db_session, user, "a")
    doc_b = _document(db_session, user, "b")
    db_session.add_all(
        [
            _chunk("x", document=doc_a),
            # Same index in a different document is fine.
            _chunk("y", document=doc_b),
        ]
    )
    db_session.flush()

    db_session.add(_chunk("dup", document_id=doc_a.id))
    expect_violation(db_session, "uq_document_chunks_document_id_chunk_index", db_session.flush)


def test_duplicate_content_hash_rejected_per_user(db_session: Session) -> None:
    alice = _user(db_session, "alice@example.com")
    bob = _user(db_session, "bob@example.com")
    _document(db_session, alice, "same-bytes")
    # Another user may upload identical bytes.
    _document(db_session, bob, "same-bytes")

    expect_violation(
        db_session,
        "uq_documents_user_id_content_hash",
        lambda: _document(db_session, alice, "same-bytes"),
    )


def test_document_requires_existing_user(db_session: Session) -> None:
    db_session.add(
        Document(
            user_id=uuid.uuid4(),
            filename="orphan.txt",
            content_type="text/plain",
            file_size=1,
            content_hash=_sha256("orphan"),
        )
    )
    expect_violation(db_session, "fk_documents_user_id_users", db_session.flush)


def test_message_requires_existing_conversation(db_session: Session) -> None:
    db_session.add(Message(conversation_id=uuid.uuid4(), role=MessageRole.USER, content="hi"))
    expect_violation(db_session, "fk_messages_conversation_id_conversations", db_session.flush)


def test_database_cascade_on_raw_user_delete(db_session: Session) -> None:
    """ON DELETE CASCADE lives in PostgreSQL, so it also holds for raw SQL deletes."""
    user = _user(db_session)
    doc = _document(db_session, user)
    doc.chunks.append(_chunk("c"))
    conv = Conversation(user=user)
    conv.messages.append(Message(role=MessageRole.ASSISTANT, content="a", citations=[]))
    db_session.add(conv)
    db_session.flush()

    db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
    db_session.expire_all()

    for model in (User, Document, DocumentChunk, Conversation, Message):
        assert _count(db_session, model) == 0, model.__name__


def test_orm_delete_document_removes_only_its_chunks(db_session: Session) -> None:
    user = _user(db_session)
    doomed = _document(db_session, user, "doomed")
    kept = _document(db_session, user, "kept")
    doomed.chunks.append(_chunk("d"))
    kept.chunks.append(_chunk("k"))
    db_session.flush()

    db_session.delete(doomed)
    db_session.flush()

    assert _count(db_session, Document) == 1
    remaining = db_session.scalars(select(DocumentChunk)).all()
    assert [c.document_id for c in remaining] == [kept.id]
    assert _count(db_session, User) == 1


@pytest.mark.parametrize(
    ("sql", "constraint"),
    [
        (
            "INSERT INTO documents (id, user_id, filename, content_type, file_size, "
            "content_hash, status) VALUES (:id, :uid, 'f', 't', 1, :hash, 'bogus')",
            "ck_documents_status",
        ),
        (
            "INSERT INTO documents (id, user_id, filename, content_type, file_size, "
            "content_hash) VALUES (:id, :uid, 'f', 't', -1, :hash)",
            "ck_documents_file_size_non_negative",
        ),
        (
            "INSERT INTO documents (id, user_id, filename, content_type, file_size, "
            "content_hash) VALUES (:id, :uid, 'f', 't', 1, 'short')",
            "ck_documents_content_hash_sha256_hex",
        ),
    ],
    ids=["status", "file_size", "content_hash"],
)
def test_document_check_constraints(db_session: Session, sql: str, constraint: str) -> None:
    user = _user(db_session)
    params = {"id": uuid.uuid4(), "uid": user.id, "hash": _sha256("x")}

    expect_violation(db_session, constraint, lambda: db_session.execute(text(sql), params))


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"chunk_index": -1}, "ck_document_chunks_chunk_index_non_negative"),
        ({"page_number": 0}, "ck_document_chunks_page_number_positive"),
        ({"char_start": -1}, "ck_document_chunks_char_range_valid"),
        ({"char_start": 5, "char_end": 5}, "ck_document_chunks_char_range_valid"),
        ({"token_count": -1}, "ck_document_chunks_token_count_non_negative"),
    ],
    ids=["chunk_index", "page_number", "char_start", "empty_range", "token_count"],
)
def test_chunk_check_constraints(
    db_session: Session, overrides: dict[str, int], constraint: str
) -> None:
    doc = _document(db_session, _user(db_session))
    db_session.add(_chunk(document=doc, **overrides))
    expect_violation(db_session, constraint, db_session.flush)


def test_chunk_embedding_round_trips_as_float_vector(db_session: Session) -> None:
    doc = _document(db_session, _user(db_session))
    vector = np.linspace(-1, 1, EMBEDDING_DIMENSION, dtype=np.float32)
    doc.chunks.append(_chunk(embedding=vector, embedding_model="intfloat/multilingual-e5-small"))
    db_session.flush()
    db_session.expire_all()

    [chunk] = db_session.get(Document, doc.id).chunks
    assert len(chunk.embedding) == EMBEDDING_DIMENSION
    np.testing.assert_allclose(np.asarray(chunk.embedding), vector, rtol=1e-6)
    assert chunk.embedding_model == "intfloat/multilingual-e5-small"


def test_chunk_embedding_with_wrong_dimension_is_rejected(db_session: Session) -> None:
    doc = _document(db_session, _user(db_session))
    db_session.add(_chunk(document=doc, embedding=np.ones(EMBEDDING_DIMENSION + 1)))
    with pytest.raises(DataError):
        db_session.flush()
    db_session.rollback()


def _job(document: Document, status: JobStatus = JobStatus.QUEUED) -> Job:
    return Job(document_id=document.id, kind=JobKind.INDEX_DOCUMENT, status=status)


def test_job_defaults(db_session: Session) -> None:
    doc = _document(db_session, _user(db_session))
    job = Job(document_id=doc.id, kind=JobKind.INDEX_DOCUMENT)
    db_session.add(job)
    db_session.flush()
    db_session.refresh(job)
    assert job.status is JobStatus.QUEUED
    assert job.attempts == 0
    assert job.run_after is not None
    assert job.locked_at is None


def test_only_one_active_job_per_document(db_session: Session) -> None:
    doc = _document(db_session, _user(db_session))
    # Finished jobs are history and do not block a new one.
    db_session.add_all([_job(doc, JobStatus.SUCCEEDED), _job(doc, JobStatus.FAILED), _job(doc)])
    db_session.flush()

    db_session.add(_job(doc, JobStatus.RUNNING))
    expect_violation(db_session, "uq_jobs_document_id_active", db_session.flush)


def test_jobs_are_deleted_with_their_document(db_session: Session) -> None:
    doc = _document(db_session, _user(db_session))
    db_session.add(_job(doc))
    db_session.flush()

    db_session.execute(text("DELETE FROM documents WHERE id = :id"), {"id": doc.id})
    assert _count(db_session, Job) == 0


def test_message_role_check_constraint(db_session: Session) -> None:
    conv = Conversation(user=_user(db_session))
    db_session.add(conv)
    db_session.flush()

    expect_violation(
        db_session,
        "ck_messages_role",
        lambda: db_session.execute(
            text(
                "INSERT INTO messages (id, conversation_id, role, content) "
                "VALUES (:id, :cid, 'system', 'x')"
            ),
            {"id": uuid.uuid4(), "cid": conv.id},
        ),
    )
