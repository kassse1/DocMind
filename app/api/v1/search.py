from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_embedder
from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.core.errors import DatabaseUnavailableRoute
from app.embeddings.base import Embedder
from app.models import User
from app.schemas.search import SearchRequest, SearchResponse, SearchResult
from app.services import search as search_service

router = APIRouter(prefix="/search", tags=["search"], route_class=DatabaseUnavailableRoute)


@router.post(
    "",
    response_model=SearchResponse,
    responses={
        401: {"description": "Authentication not configured (prod)"},
        404: {"description": "A document in document_ids does not exist or is not yours"},
        422: {"description": "Empty or too long query, top_k out of range, invalid filter"},
        503: {"description": "Database or embedding model unavailable"},
    },
)
def search(
    body: SearchRequest,
    session: Annotated[Session, Depends(get_db_session)],
    user: Annotated[User, Depends(get_current_user)],
    embedder: Annotated[Embedder, Depends(get_embedder)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SearchResponse:
    """Semantic search over your indexed documents: the passages closest to the query.

    POST rather than GET keeps the (possibly private) query out of URLs and access logs.
    An empty result list means no indexed chunk matched, not an error.
    """
    hits = search_service.search(session, embedder, settings, user, body)
    return SearchResponse(
        results=[SearchResult(**asdict(hit)) for hit in hits],
        embedding_model=embedder.model_name,
    )
