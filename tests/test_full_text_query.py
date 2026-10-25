"""Full-text query construction without a database."""

import re
import uuid

from sqlalchemy.dialects import postgresql

from app.models.document import FULL_TEXT_CONFIGS, FULL_TEXT_VECTOR_SQL
from app.retrieval.full_text_search import build_full_text_query

USER_ID = uuid.uuid4()


def _compile(statement) -> tuple[str, dict]:
    compiled = statement.compile(dialect=postgresql.dialect())
    return re.sub(r"::\w+", "", " ".join(str(compiled).split())), compiled.params


def test_vector_expression_combines_simple_and_russian() -> None:
    assert FULL_TEXT_CONFIGS == ("simple", "russian")
    assert FULL_TEXT_VECTOR_SQL == (
        "to_tsvector('simple'::regconfig, content) || to_tsvector('russian'::regconfig, content)"
    )


def test_query_text_is_only_ever_a_bound_parameter() -> None:
    hostile = "x'); DROP TABLE users; --"
    sql, params = _compile(
        build_full_text_query(hostile, user_id=USER_ID, embedding_model="m", limit=5)
    )
    assert "DROP TABLE" not in sql
    assert [v for v in params.values() if v == hostile] == [hostile, hostile]  # one per config


def test_query_shape_filters_ranks_and_breaks_ties() -> None:
    doc = uuid.uuid4()
    sql, params = _compile(
        build_full_text_query(
            "invoice", user_id=USER_ID, embedding_model="m", limit=7, document_ids=[doc]
        )
    )
    assert "plainto_tsquery('simple'" in sql and "plainto_tsquery('russian'" in sql
    assert "(document_chunks.search_vector @@ tsq.q)" in sql
    assert "documents.user_id = %(user_id_1)s" in sql
    assert "document_chunks.embedding_model = %(embedding_model_1)s" in sql
    assert "document_chunks.document_id IN" in sql
    assert sql.endswith(
        "ORDER BY score DESC, document_chunks.document_id, document_chunks.chunk_index"
        " LIMIT %(param_1)s"
    )
    assert params["replace_1"] == " & " and params["replace_2"] == " | "  # any-term matching
    assert (params["user_id_1"], params["param_1"], params["document_id_1"]) == (
        USER_ID,
        7,
        [doc],
    )


def test_no_document_filter_when_not_given() -> None:
    sql, _ = _compile(build_full_text_query("q", user_id=USER_ID, embedding_model="m", limit=5))
    assert " IN " not in sql
