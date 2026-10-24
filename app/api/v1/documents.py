from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_storage
from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.ingestion.storage import FileStorage
from app.models import User
from app.schemas.documents import DocumentRead, DocumentUploadResponse, ExtractionSummary
from app.services import documents as document_service

router = APIRouter(prefix="/documents", tags=["documents"])


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
