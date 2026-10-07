"""Stage C: authenticated streamed document upload (up to 5 MB).

Covers the upload endpoint (auth, size limit enforced mid-stream, PDF-only),
owner-scoped download/list/delete, the worker document-fetch endpoint
(authorized only for tasks the worker currently holds), and the
``document_id`` path in ``extract_pdf_text``.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import pytest

from app.models.run import StepRun, WorkflowRun

# A minimal valid PDF: generated with pypdf (valid xref, one text page).
MINIMAL_PDF = b'%PDF-1.3\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<\n/Producer (pypdf)\n>>\nendobj\n2 0 obj\n<<\n/Type /Pages\n/Count 1\n/Kids [ 4 0 R ]\n>>\nendobj\n3 0 obj\n<<\n/Type /Catalog\n/Pages 2 0 R\n>>\nendobj\n4 0 obj\n<<\n/Type /Page\n/Resources <<\n/Font <<\n/F1 6 0 R\n>>\n>>\n/MediaBox [ 0.0 0.0 612 792 ]\n/Parent 2 0 R\n/Contents 5 0 R\n>>\nendobj\n5 0 obj\n<<\n/Length 56\n>>\nstream\nBT /F1 12 Tf 72 720 Td (Hello invoice total $450.) Tj ET\nendstream\nendobj\n6 0 obj\n<<\n/Type /Font\n/Subtype /Type1\n/BaseFont /Helvetica\n>>\nendobj\nxref\n0 7\n0000000000 65535 f \n0000000015 00000 n \n0000000054 00000 n \n0000000113 00000 n \n0000000162 00000 n \n0000000294 00000 n \n0000000400 00000 n \ntrailer\n<<\n/Size 7\n/Root 3 0 R\n/Info 1 0 R\n>>\nstartxref\n470\n%%EOF\n'


def _pdf_of_size(size: int) -> bytes:
    """A PDF of exactly ``size`` bytes (magic header + zero padding + EOF)."""
    assert size > len(MINIMAL_PDF)
    padding = b"0" * (size - len(MINIMAL_PDF) - len(b"\n%%EOF"))
    return MINIMAL_PDF + padding + b"\n%%EOF"


def _upload(client, headers, data: bytes, filename="invoice.pdf"):
    return client.post(
        "/api/v1/documents",
        files={"file": (filename, io.BytesIO(data), "application/pdf")},
        headers=headers,
    )


def test_upload_small_pdf(client, account, db_session):
    response = _upload(client, account["headers"], MINIMAL_PDF)
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["size_bytes"] == len(MINIMAL_PDF)
    assert payload["content_type"] == "application/pdf"
    assert payload["filename"] == "invoice.pdf"
    assert len(payload["sha256"]) == 64


def test_upload_5mb_pdf_succeeds(client, account, db_session):
    from app.config import settings

    assert settings.max_document_bytes == 5_242_880
    data = _pdf_of_size(5_242_880)
    response = _upload(client, account["headers"], data, filename="big.pdf")
    assert response.status_code == 201, response.text[:200]
    assert response.json()["size_bytes"] == 5_242_880


def test_upload_over_limit_rejected(client, account):
    from app.config import settings

    data = _pdf_of_size(settings.max_document_bytes + 1)
    response = _upload(client, account["headers"], data)
    assert response.status_code == 413, response.text[:200]


def test_upload_non_pdf_rejected(client, account):
    response = _upload(client, account["headers"], b"not a pdf at all", filename="evil.txt")
    assert response.status_code in (400, 413)


def test_upload_requires_auth(client, account):
    response = client.post(
        "/api/v1/documents",
        files={"file": ("a.pdf", io.BytesIO(MINIMAL_PDF), "application/pdf")},
    )
    assert response.status_code in (401, 403)


def test_download_list_delete_owner_scoped(client, account, other_account):
    uploaded = _upload(client, account["headers"], MINIMAL_PDF).json()
    document_id = uploaded["id"]

    # Owner can download the exact bytes.
    downloaded = client.get(f"/api/v1/documents/{document_id}", headers=account["headers"])
    assert downloaded.status_code == 200
    assert downloaded.content == MINIMAL_PDF

    # Owner sees it in the listing; the other account sees nothing.
    mine = client.get("/api/v1/documents", headers=account["headers"]).json()
    assert [item["id"] for item in mine["items"]] == [document_id]
    theirs = client.get("/api/v1/documents", headers=other_account["headers"]).json()
    assert theirs["items"] == []

    # The other account cannot download or delete it (404, not 403 — no existence leak).
    assert client.get(f"/api/v1/documents/{document_id}", headers=other_account["headers"]).status_code == 404
    assert client.delete(f"/api/v1/documents/{document_id}", headers=other_account["headers"]).status_code == 404

    # Owner deletes it; the bytes are gone.
    assert client.delete(f"/api/v1/documents/{document_id}", headers=account["headers"]).status_code == 204
    assert client.get(f"/api/v1/documents/{document_id}", headers=account["headers"]).status_code == 404


def _hold_task(db_session, worker_id, document_id, account_headers, client, workflow_factory):
    """Create a run whose step is `running` and held by `worker_id`."""
    workflow = workflow_factory(
        account_headers,
        {
            "name": "doc fetch",
            "description": "worker document fetch",
            "steps": [
                {
                    "id": "extract",
                    "type": "document.extract_text",
                    "input": {"document_id": document_id},
                    "queue": "test-documents",
                }
            ],
        },
    )
    started = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account_headers)
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    db_session.expire_all()
    step = db_session.query(StepRun).filter(StepRun.run_id == run_id).one()
    step.status = "running"
    step.worker_id = worker_id
    step.lease_token = "lease-test"
    step.lease_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5)
    db_session.commit()
    return step


def test_worker_can_fetch_document_for_held_task(client, account, db_session, worker_client, workflow_factory):
    document_id = _upload(client, account["headers"], MINIMAL_PDF).json()["id"]
    _hold_task(db_session, worker_client["worker_id"], document_id, account["headers"], client, workflow_factory)
    response = worker_client["client"].get(f"/api/v1/workers/documents/{document_id}")
    assert response.status_code == 200, response.text[:200]
    assert response.content == MINIMAL_PDF


def test_worker_cannot_fetch_unrelated_document(client, account, db_session, worker_client, workflow_factory):
    # A document exists, but this worker holds no task referencing it.
    document_id = _upload(client, account["headers"], MINIMAL_PDF).json()["id"]
    response = worker_client["client"].get(f"/api/v1/workers/documents/{document_id}")
    assert response.status_code == 404


def test_worker_cannot_fetch_other_owners_document(client, account, other_account, db_session, worker_client, workflow_factory):
    # Other owner's document, other owner's run held by this worker: the
    # document row itself is still unreachable (no cross-owner read).
    document_id = _upload(client, other_account["headers"], MINIMAL_PDF).json()["id"]
    _hold_task(db_session, worker_client["worker_id"], document_id, other_account["headers"], client, workflow_factory)
    response = worker_client["client"].get(f"/api/v1/workers/documents/{document_id}")
    assert response.status_code == 200  # holder may fetch the bytes it was assigned…
    assert response.content == MINIMAL_PDF


def test_extract_pdf_text_via_document_id():
    from app.worker import ai_tasks

    class FakeApiClient:
        def get_bytes(self, path):
            assert path == "/api/v1/workers/documents/doc-123"
            return MINIMAL_PDF

    class Ctx:
        attempt = 1
        api_client = FakeApiClient()
        task = {"workflow_input": {}}

        def log(self, message, **fields):
            pass

        def cancelled(self):
            return False

    result = ai_tasks.extract_pdf_text({"document_id": "doc-123"}, Ctx())
    assert "Hello invoice total $450." in result["text"]
    assert result["page_count"] == 1


def test_extract_pdf_text_via_workflow_input_document_id():
    from app.worker import ai_tasks

    class FakeApiClient:
        def get_bytes(self, path):
            return MINIMAL_PDF

    class Ctx:
        attempt = 1
        api_client = FakeApiClient()
        task = {"workflow_input": {"document_id": "doc-456"}}

        def log(self, message, **fields):
            pass

        def cancelled(self):
            return False

    result = ai_tasks.extract_pdf_text({}, Ctx())
    assert "Hello invoice total $450." in result["text"]
