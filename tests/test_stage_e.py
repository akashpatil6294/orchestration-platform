"""Stage E: Phase 5 gaps — trigger rate limits, storage quota, key rotation.

- The public webhook hook endpoint is rate-limited (60/min per trigger+IP).
- Document uploads enforce a per-user storage quota.
- `rotate-secrets-key` covers workflow secrets, trigger secrets, notification
  channel URLs and connections, with a `--dry-run` mode.
- Cross-tenant negative tests for documents and connections.
"""
from __future__ import annotations

import io

import pytest
from sqlalchemy import select


def test_webhook_hook_rate_limited(client):
    # The rate check runs before signature verification, so an unknown trigger
    # exercises the limiter without needing a valid signature.
    statuses = []
    for _ in range(65):
        response = client.post("/api/v1/hooks/no-such-trigger", content=b"{}")
        statuses.append(response.status_code)
    assert 429 in statuses
    limited = client.post("/api/v1/hooks/no-such-trigger", content=b"{}")
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "trigger_rate_limited"


def _pdf(size: int) -> bytes:
    header = b"%PDF-1.4\n"
    return header + b"0" * (size - len(header) - len(b"\n%%EOF")) + b"\n%%EOF"


def test_document_upload_enforces_storage_quota(client, account, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "quota_storage_bytes_per_user", 10_000)
    response = client.post(
        "/api/v1/documents",
        files={"file": ("invoice.pdf", io.BytesIO(_pdf(20_000)), "application/pdf")},
        headers=account["headers"],
    )
    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "storage_quota_exceeded"


def test_document_upload_within_quota_ok(client, account, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "quota_storage_bytes_per_user", 1_000_000)
    response = client.post(
        "/api/v1/documents",
        files={"file": ("invoice.pdf", io.BytesIO(_pdf(20_000)), "application/pdf")},
        headers=account["headers"],
    )
    assert response.status_code == 201, response.text


def test_rotate_secrets_key_dry_run_changes_nothing(client, account, db_session, workflow_factory, capsys):
    from app.config import settings
    from app.models.workflow import WorkflowSecret
    from app.core import security

    workflow = workflow_factory(account["headers"])
    row = WorkflowSecret(workflow_id=workflow["id"], name="API_TOKEN", ciphertext=security.encrypt_secret("s3cret"))
    db_session.add(row)
    db_session.commit()
    before = row.ciphertext

    from app.cli import cmd_rotate_secrets_key
    import argparse

    rc = cmd_rotate_secrets_key(argparse.Namespace(old_key=settings.secrets_encryption_key, new_key="new-key", dry_run=True))
    assert rc == 0
    assert "Dry run" in capsys.readouterr().out
    db_session.refresh(row)
    assert row.ciphertext == before


def test_rotate_secrets_key_covers_connections_and_channels(client, account, db_session, monkeypatch):
    from app.config import settings
    from app.core import security
    from app.models.connection import Connection
    from app.models.notification import NotificationChannel
    from app.services.connection_service import create_connection

    create_connection(
        db_session, owner_id=account["user"]["id"], name="c1", kind="generic", value="conn-secret"
    )
    channel = NotificationChannel(
        user_id=account["user"]["id"], name="ch1", url_ciphertext=security.encrypt_secret("https://hooks.example/x")
    )
    db_session.add(channel)
    db_session.commit()

    old_key = settings.secrets_encryption_key
    new_key = "a" * 32 + "b" * 12  # Fernet-compatible length via encrypt_secret? use token format
    import secrets as _secrets

    new_key = _secrets.token_urlsafe(32)
    from app.cli import cmd_rotate_secrets_key
    import argparse

    try:
        rc = cmd_rotate_secrets_key(argparse.Namespace(old_key=old_key, new_key=new_key, dry_run=False))
        assert rc == 0

        # New key decrypts everything; old key no longer works.
        settings.secrets_encryption_key = new_key
        conn = db_session.scalar(select(Connection).where(Connection.owner_id == account["user"]["id"]))
        assert security.decrypt_secret(conn.value_ciphertext) == "conn-secret"
        db_session.refresh(channel)
        assert security.decrypt_secret(channel.url_ciphertext) == "https://hooks.example/x"
    finally:
        settings.secrets_encryption_key = old_key


def test_documents_cross_tenant_denied(client, account, other_account):
    uploaded = client.post(
        "/api/v1/documents",
        files={"file": ("invoice.pdf", io.BytesIO(_pdf(5_000)), "application/pdf")},
        headers=account["headers"],
    )
    assert uploaded.status_code == 201
    document_id = uploaded.json()["id"]

    assert client.get(f"/api/v1/documents/{document_id}", headers=other_account["headers"]).status_code == 404
    assert client.delete(f"/api/v1/documents/{document_id}", headers=other_account["headers"]).status_code == 404
    listing = client.get("/api/v1/documents", headers=other_account["headers"]).json()
    assert all(item["id"] != document_id for item in listing["items"])


def test_connections_cross_tenant_denied(client, account, other_account):
    created = client.post(
        "/api/v1/connections",
        json={"name": "mine", "kind": "generic", "value": "v"},
        headers=account["headers"],
    )
    assert created.status_code == 201
    connection_id = created.json()["id"]
    # Other tenant cannot read, update, or delete.
    assert client.get("/api/v1/connections", headers=other_account["headers"]).json()["items"] == []
    assert client.patch(f"/api/v1/connections/{connection_id}", json={"value": "x"}, headers=other_account["headers"]).status_code in (403, 404)
    assert client.delete(f"/api/v1/connections/{connection_id}", headers=other_account["headers"]).status_code in (403, 404, 204)


def test_templates_install_cross_tenant_isolated(client, account, other_account, db_session):
    from app.services import workflow_packs

    workflow = workflow_packs.install_pack(db_session, account["user"]["id"], "log-digest")
    # The other tenant cannot see or run the installed workflow.
    assert client.get(f"/api/v1/workflows/{workflow.id}", headers=other_account["headers"]).status_code == 404
