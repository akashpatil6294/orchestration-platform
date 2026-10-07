"""Authenticated document upload/download.

Uploads are multipart and stream to storage in chunks; the size limit is
enforced while reading, so an over-limit file is rejected with 413 before it
is fully buffered. Documents are owner-scoped: listing, download and delete
only ever touch the requesting user's own documents.
"""
from __future__ import annotations

from fastapi import APIRouter, File, UploadFile, status
from fastapi.responses import Response

from app.api.deps import CurrentUser, DbSession
from app.services import document_service

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])

_ALLOWED_CONTENT_TYPES = {"application/pdf"}


@router.post("", status_code=status.HTTP_201_CREATED)
def upload_document(user: CurrentUser, db: DbSession, file: UploadFile = File(...)) -> dict:
    """Upload a PDF document (up to the configured document size limit)."""
    filename = file.filename or "document.pdf"
    content_type = (file.content_type or "application/pdf").split(";")[0].strip().lower()
    if content_type not in _ALLOWED_CONTENT_TYPES:
        from app.core.errors import PayloadTooLarge

        raise PayloadTooLarge("Only PDF documents are accepted", code="not_a_pdf")
    document = document_service.store_upload(
        db,
        owner_id=user.id,
        filename=filename,
        content_type=content_type,
        stream=file.file,
    )
    return document_service.document_view(document)


@router.get("")
def list_documents(user: CurrentUser, db: DbSession) -> dict:
    documents = document_service.list_for_owner(db, user.id)
    return {"items": [document_service.document_view(document) for document in documents]}


@router.get("/{document_id}")
def download_document(document_id: str, user: CurrentUser, db: DbSession) -> Response:
    document = document_service.get_for_owner(db, document_id, user.id)
    data = document_service.open_bytes(document)
    return Response(
        content=data,
        media_type=document.content_type,
        headers={"Content-Disposition": f'attachment; filename="{document.filename or "document.pdf"}"'},
    )


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: str, user: CurrentUser, db: DbSession) -> None:
    document = document_service.get_for_owner(db, document_id, user.id)
    document_service.delete_document(db, document)


__all__ = ["router"]
