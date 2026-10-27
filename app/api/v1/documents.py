import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_storage
from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.core.errors import DatabaseUnavailableRoute
from app.ingestion.storage import FileStorage
from app.models import User
from app.schemas.documents import (
    DocumentList,
    DocumentRead,
    DocumentUploadResponse,
    ExtractionSummary,
)
from app.services import documents as document_service

# Database outages answer 503 database_unavailable like the other data routers, not 500.
router = APIRouter(prefix="/documents", tags=["documents"], route_class=DatabaseUnavailableRoute)

DEFAULT_PAGE = 50
MAX_PAGE = 200


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=DocumentUploadResponse,
    responses={
        400: {"description": "Empty file or malformed multipart body"},
        401: {"description": "Authentication not configured (prod)"},
        409: {"description": "The same file was already uploaded by this user"},
        413: {"description": "File exceeds the upload size limit"},
        415: {"description": "Unsupported extension or conflicting Content-Type"},
        422: {"description": "Content invalid, corrupt, encrypted or without text"},
    },
)
def upload_document(
    file: Annotated[UploadFile, File(description="PDF, DOCX or TXT file")],
    session: Annotated[Session, Depends(get_db_session)],
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[FileStorage, Depends(get_storage)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DocumentUploadResponse:
    """Upload a document. It is validated and parsed now; a background worker indexes it."""
    result = document_service.upload_document(session, storage, settings, user, file)
    return DocumentUploadResponse(
        document=DocumentRead.model_validate(result.document),
        extraction=ExtractionSummary(
            page_count=result.extraction.page_count,
            pages_with_text=len(result.extraction.pages),
            character_count=result.extraction.character_count,
        ),
    )


@router.get(
    "",
    response_model=DocumentList,
    responses={401: {"description": "Missing, invalid or expired access token"}},
)
def list_documents(
    session: Annotated[Session, Depends(get_db_session)],
    user: Annotated[User, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> DocumentList:
    """Your documents, newest first, with their indexing status."""
    documents = document_service.list_documents(session, user, limit, offset)
    return DocumentList(
        documents=[DocumentRead.model_validate(d) for d in documents], limit=limit, offset=offset
    )


@router.get(
    "/{document_id}",
    response_model=DocumentRead,
    responses={
        401: {"description": "Missing, invalid or expired access token"},
        404: {"description": "No such document, or it belongs to someone else (same response)"},
    },
)
def get_document(
    document_id: uuid.UUID,
    session: Annotated[Session, Depends(get_db_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> DocumentRead:
    return DocumentRead.model_validate(document_service.get_document(session, user, document_id))


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {"description": "Missing, invalid or expired access token"},
        404: {"description": "No such document, or it belongs to someone else (same response)"},
    },
)
def delete_document(
    document_id: uuid.UUID,
    session: Annotated[Session, Depends(get_db_session)],
    user: Annotated[User, Depends(get_current_user)],
    storage: Annotated[FileStorage, Depends(get_storage)],
) -> Response:
    """Delete one of your documents with its chunks, embeddings, jobs and stored file."""
    document_service.delete_document(session, storage, user, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
