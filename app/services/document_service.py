"""Owner-scoped document storage for multi-megabyte uploads.

Files stream to disk in chunks — the size limit is enforced *while* reading so
an over-limit upload is rejected with 413 before its bytes are ever fully
buffered. Only the metadata row lives in the database; the bytes go to the
configured artifact backend (local disk or S3), namespaced under
``documents/`` so they never collide with run artifacts.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.errors import NotFound, PayloadTooLarge
from app.models.base import new_id, utcnow
from app.models.document import Document

_CHUNK_SIZE = 65_536
_PDF_MAGIC = b"%PDF-"


class DocumentTooLarge(PayloadTooLarge):
    """Raised mid-stream as soon as the byte limit is exceeded."""


def store_upload(
    db: Session,
    *,
    owner_id: str,
    filename: str,
    content_type: str,
    stream: BinaryIO,
) -> Document:
    """Stream an upload to storage, enforcing the size limit as bytes arrive."""
    limit = settings.max_document_bytes
    document_id = new_id()
    digest = hashlib.sha256()
    total = 0
    first_chunk: bytes | None = None

    tmp_file = tempfile.NamedTemporaryFile(prefix=f"doc-{document_id}-", delete=False)
    try:
        while True:
            chunk = stream.read(_CHUNK_SIZE)
            if not chunk:
                break
            if first_chunk is None:
                first_chunk = chunk[:8]
            total += len(chunk)
            if total > limit:
                raise DocumentTooLarge(
                    f"Document is larger than the {limit}-byte limit",
                    details={"limit": limit},
                )
            digest.update(chunk)
            tmp_file.write(chunk)
    except Exception:
        tmp_file.close()
        os.unlink(tmp_file.name)
        raise
    tmp_file.close()

    if total == 0:
        os.unlink(tmp_file.name)
        raise PayloadTooLarge("Uploaded document is empty", details={"limit": limit})
    if not (first_chunk or b"").startswith(_PDF_MAGIC):
        os.unlink(tmp_file.name)
        raise PayloadTooLarge("Only PDF documents are accepted", code="not_a_pdf")

    quota = settings.quota_storage_bytes_per_user
    if quota > 0:
        current = db.scalar(
            select(func.coalesce(func.sum(Document.size_bytes), 0)).where(Document.owner_id == owner_id)
        ) or 0
        if current + total > quota:
            os.unlink(tmp_file.name)
            raise PayloadTooLarge(
                f"Storage quota exceeded ({current + total} > {quota} bytes)",
                code="storage_quota_exceeded",
                details={"limit": quota, "used_bytes": current, "incoming_bytes": total},
            )

    backend = settings.artifact_storage
    storage_key = _persist(backend, document_id, tmp_file.name)
    os.unlink(tmp_file.name)

    document = Document(
        id=document_id,
        owner_id=owner_id,
        filename=(filename or "")[:255],
        content_type=(content_type or "application/pdf")[:127],
        size_bytes=total,
        sha256=digest.hexdigest(),
        storage_backend=backend,
        storage_key=storage_key,
        doc_metadata={},
        created_at=utcnow(),
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


def get_for_owner(db: Session, document_id: str, owner_id: str) -> Document:
    document = db.scalar(select(Document).where(Document.id == document_id, Document.owner_id == owner_id))
    if document is None:
        raise NotFound("Document not found", code="document_not_found")
    return document


def list_for_owner(db: Session, owner_id: str) -> list[Document]:
    return list(
        db.scalars(select(Document).where(Document.owner_id == owner_id).order_by(Document.created_at.desc())).all()
    )


def open_bytes(document: Document) -> bytes:
    """Read the stored bytes (local disk or S3)."""
    if document.storage_backend == "local":
        root = Path(settings.artifact_dir).resolve()
        path = Path(document.storage_key).resolve()
        if root not in path.parents:
            raise NotFound("Document data is unavailable", code="document_not_found")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise NotFound("Document data is unavailable", code="document_not_found") from exc
    if document.storage_backend == "s3":
        from app.services.artifact_service import _s3_client

        try:
            result = _s3_client().get_object(Bucket=settings.artifact_s3_bucket, Key=document.storage_key)
            return result["Body"].read()
        except Exception as exc:
            raise NotFound("Document data is unavailable", code="document_not_found") from exc
    raise NotFound("Document backend is unavailable", code="document_not_found")


def delete_document(db: Session, document: Document) -> None:
    try:
        if document.storage_backend == "local":
            root = Path(settings.artifact_dir).resolve()
            path = Path(document.storage_key).resolve()
            if root in path.parents and path.is_file():
                path.unlink()
        elif document.storage_backend == "s3":
            from app.services.artifact_service import _s3_client

            _s3_client().delete_object(Bucket=settings.artifact_s3_bucket, Key=document.storage_key)
    except Exception:
        pass  # best effort; the metadata row is the source of truth
    db.delete(document)
    db.commit()


def document_view(document: Document) -> dict:
    return {
        "id": document.id,
        "filename": document.filename,
        "content_type": document.content_type,
        "size_bytes": document.size_bytes,
        "sha256": document.sha256,
        "created_at": document.created_at,
    }


def _persist(backend: str, document_id: str, tmp_path: str) -> str:
    if backend == "local":
        root = (Path(settings.artifact_dir) / "documents").resolve()
        root.mkdir(parents=True, exist_ok=True)
        dest = (root / document_id).resolve()
        if root not in dest.parents:
            raise ValueError("Document path escaped its configured directory")
        # Same-filesystem move (tempfile may live on another mount, so copy
        # through a temp file inside the destination directory, then rename
        # atomically — no partial file is ever visible).
        import shutil

        staging = root / f".{document_id}.tmp"
        shutil.copyfile(tmp_path, staging)
        os.replace(staging, dest)
        return str(dest)
    if backend == "s3":
        from app.services.artifact_service import _s3_client

        key = f"documents/{document_id}"
        with open(tmp_path, "rb") as handle:
            _s3_client().put_object(
                Bucket=settings.artifact_s3_bucket,
                Key=key,
                Body=handle,
                ContentType="application/pdf",
                ServerSideEncryption="AES256",
            )
        return key
    raise ValueError(f"Unknown artifact storage backend: {backend}")


__all__ = [
    "DocumentTooLarge",
    "delete_document",
    "document_view",
    "get_for_owner",
    "list_for_owner",
    "open_bytes",
    "store_upload",
]
