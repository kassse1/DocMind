import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import DocumentStatus


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    content_type: str
    file_size: int
    content_hash: str
    status: DocumentStatus
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class ExtractionSummary(BaseModel):
    """Computed while validating the upload; not stored (Phase 4 re-extracts for indexing)."""

    page_count: int | None
    pages_with_text: int
    character_count: int


class DocumentList(BaseModel):
    documents: list[DocumentRead]
    limit: int
    offset: int


class DocumentUploadResponse(BaseModel):
    document: DocumentRead
    extraction: ExtractionSummary
