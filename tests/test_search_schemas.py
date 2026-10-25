import uuid

import pytest
from pydantic import ValidationError

from app.schemas.search import (
    DEFAULT_TOP_K,
    MAX_DOCUMENT_FILTER,
    MAX_QUERY_CHARS,
    MAX_TOP_K,
    SearchRequest,
)


def test_defaults() -> None:
    request = SearchRequest(query="payment terms")
    assert (request.top_k, request.document_ids, request.min_similarity) == (
        DEFAULT_TOP_K,
        None,
        None,
    )


def test_query_is_stripped() -> None:
    assert SearchRequest(query="  Келісімшарт  \n").query == "Келісімшарт"


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_empty_query_is_rejected(query: str) -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query=query)


def test_query_length_limit() -> None:
    SearchRequest(query="x" * MAX_QUERY_CHARS)
    with pytest.raises(ValidationError):
        SearchRequest(query="x" * (MAX_QUERY_CHARS + 1))


@pytest.mark.parametrize("top_k", [0, -1, MAX_TOP_K + 1])
def test_top_k_out_of_range_is_rejected(top_k: int) -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="q", top_k=top_k)


def test_top_k_maximum_is_accepted() -> None:
    assert SearchRequest(query="q", top_k=MAX_TOP_K).top_k == MAX_TOP_K


def test_document_ids_are_deduplicated_in_order() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    request = SearchRequest(query="q", document_ids=[a, b, a])
    assert request.document_ids == [a, b]


@pytest.mark.parametrize(
    "document_ids",
    [[], ["not-a-uuid"], [str(uuid.uuid4())] * (MAX_DOCUMENT_FILTER + 1)],
)
def test_invalid_document_filter_is_rejected(document_ids: list[str]) -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="q", document_ids=document_ids)


@pytest.mark.parametrize("value", [-1.01, 1.01])
def test_min_similarity_outside_cosine_range_is_rejected(value: float) -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="q", min_similarity=value)


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="q", topk=3)
