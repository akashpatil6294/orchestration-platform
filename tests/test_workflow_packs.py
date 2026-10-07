"""Stage D: workflow packs, templates gallery, team connections, E2E runs.

- Every pack lists in the gallery, installs idempotently, and its definition
  validates.
- Connections are write-only values, owner/team-scoped, resolved at dispatch.
- E2E: a pack runs happy-path to `succeeded` with the embedded worker (local
  HTTP stub + mocked AI + dry-run email); a pack run without its connection
  fails fast at start with `connection_missing`.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.services import workflow_packs
from app.services.connection_service import (
    create_connection,
    delete_connection,
    get_for_user,
    list_for_user,
    resolve_connections,
)


def _definition(pack_id):
    pack = workflow_packs.get_pack(pack_id)
    assert pack is not None
    return pack["definition"]


def test_gallery_lists_seven_packs(client):
    response = client.get("/api/v1/templates")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 7
    ids = {item["id"] for item in items}
    assert ids == {
        "incident-triage",
        "deploy-pipeline",
        "db-migration",
        "invoice-processing",
        "log-digest",
        "vuln-scan",
        "backup-verify",
    }
    for item in items:
        assert item["name"] and item["description"] and item["category"]
        assert isinstance(item["connections_required"], list)
        assert item["step_count"] >= 4


def test_all_pack_definitions_validate():
    from app.services.workflow_service import validate_definition

    for pack in workflow_packs.PACKS:
        result = validate_definition(dict(pack["definition"]))
        assert result.get("valid", True), f"{pack['id']}: {result}"


def test_install_pack_creates_and_publishes(client, account, db_session):
    response = client.post("/api/v1/templates/log-digest/install", headers=account["headers"])
    assert response.status_code == 201, response.text
    workflow = response.json()
    assert workflow["name"] == "Error log digest"
    assert workflow["latest_version"] >= 1


def test_install_pack_idempotent(client, account):
    first = client.post("/api/v1/templates/log-digest/install", headers=account["headers"]).json()
    second = client.post("/api/v1/templates/log-digest/install", headers=account["headers"]).json()
    assert first["id"] == second["id"]


def test_install_unknown_pack_404(client, account):
    response = client.post("/api/v1/templates/nope/install", headers=account["headers"])
    assert response.status_code == 404


def test_connections_crud_value_never_returned(client, account, db_session):
    created = client.post(
        "/api/v1/connections",
        json={"name": "team-slack", "kind": "slack_webhook", "value": "https://hooks.slack.com/services/T/B/X"},
        headers=account["headers"],
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["name"] == "team-slack"
    assert "value" not in payload
    assert "ciphertext" not in str(payload)

    listing = client.get("/api/v1/connections", headers=account["headers"]).json()
    assert [item["name"] for item in listing["items"]] == ["team-slack"]
    assert all("value" not in item for item in listing["items"])

    # Invalid kind rejected; bad slack URL rejected.
    bad = client.post(
        "/api/v1/connections",
        json={"name": "x", "kind": "nope", "value": "v"},
        headers=account["headers"],
    )
    assert bad.status_code == 422
    bad_url = client.post(
        "/api/v1/connections",
        json={"name": "y", "kind": "slack_webhook", "value": "https://evil.example.com/hook"},
        headers=account["headers"],
    )
    assert bad_url.status_code == 422

    # Duplicate name rejected.
    dup = client.post(
        "/api/v1/connections",
        json={"name": "team-slack", "kind": "generic", "value": "v"},
        headers=account["headers"],
    )
    assert dup.status_code == 409

    connection_id = payload["id"]
    deleted = client.delete(f"/api/v1/connections/{connection_id}", headers=account["headers"])
    assert deleted.status_code == 204
    assert client.get("/api/v1/connections", headers=account["headers"]).json()["items"] == []


def test_connections_are_owner_scoped(client, account, other_account):
    client.post(
        "/api/v1/connections",
        json={"name": "mine", "kind": "generic", "value": "secret-value"},
        headers=account["headers"],
    )
    listing = client.get("/api/v1/connections", headers=other_account["headers"]).json()
    assert listing["items"] == []


def test_resolve_connections_substitutes_value(client, account, db_session):
    create_connection(
        db_session,
        owner_id=account["user"]["id"],
        name="team-slack",
        kind="slack_webhook",
        value="https://hooks.slack.com/services/T/B/X",
    )
    workflow = workflow_packs.install_pack(db_session, account["user"]["id"], "backup-verify")
    resolved, paths = resolve_connections(db_session, workflow.id, {"webhook_url": {"$connection": "team-slack"}})
    assert resolved == {"webhook_url": "https://hooks.slack.com/services/T/B/X"}
    assert paths == ["webhook_url"]


def test_resolve_connections_missing_raises(client, account, db_session):
    from app.core.errors import Invalid

    workflow = workflow_packs.install_pack(db_session, account["user"]["id"], "backup-verify")
    with pytest.raises(Invalid) as exc_info:
        resolve_connections(db_session, workflow.id, {"webhook_url": {"$connection": "nope"}})
    assert exc_info.value.code == "connection_missing"


def test_run_start_fails_fast_on_missing_connection(client, account, db_session):
    workflow = workflow_packs.install_pack(db_session, account["user"]["id"], "backup-verify")
    response = client.post(
        f"/api/v1/workflows/{workflow.id}/runs",
        json={"input": {"backup_trigger_url": "http://x", "backup_verify_url": "http://x", "backup_target": "db"}},
        headers=account["headers"],
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "connection_missing"


# --------------------------------------------------------------------------- #
# End-to-end with the embedded worker
# --------------------------------------------------------------------------- #


class _LogHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ERROR db connection pool exhausted\nERROR timeout on /api/orders\nINFO ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def log_server():
    server = HTTPServer(("127.0.0.1", 0), _LogHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/logs"
    server.shutdown()


@pytest.fixture()
def embedded_worker(client, monkeypatch):
    from app.config import settings
    from app.main import app
    from app.worker.embedded import EmbeddedWorker

    monkeypatch.setattr(settings, "embedded_worker_enabled", True)
    monkeypatch.setattr(settings, "embedded_worker_concurrency", 2)
    monkeypatch.setattr(settings, "worker_long_poll_seconds", 0)
    handle = EmbeddedWorker(app)
    handle.start()
    yield handle
    handle.stop()


def _mock_ai(monkeypatch):
    from app.worker import ai_tasks

    def fake_generate(prompt, ctx, *, json_response=False):
        if json_response:
            return json.dumps({"category": "infrastructure", "reason": "db pool errors", "confidence": 0.9}), None
        return "Digest: db pool exhausted twice, one timeout. Action: scale pool.", None

    monkeypatch.setattr(ai_tasks, "_generate", fake_generate)


def test_pack_happy_path_log_digest(client, account, db_session, monkeypatch, embedded_worker, log_server):
    from app.config import settings

    _mock_ai(monkeypatch)
    monkeypatch.setattr(settings, "connector_allow_private_networks", True)
    monkeypatch.setattr(settings, "connectors_dry_run", True)

    workflow = workflow_packs.install_pack(db_session, account["user"]["id"], "log-digest")
    started = client.post(
        f"/api/v1/workflows/{workflow.id}/runs",
        json={"input": {"logs_url": log_server, "digest_recipients": ["ops@example.com"]}},
        headers=account["headers"],
    )
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]

    import time

    deadline = time.monotonic() + 60.0
    final = None
    while time.monotonic() < deadline:
        detail = client.get(f"/api/v1/runs/{run_id}", headers=account["headers"]).json()
        if detail["status"] in ("succeeded", "failed", "cancelled"):
            final = detail
            break
        time.sleep(1.0)
    assert final is not None, "run did not finish"
    assert final["status"] == "succeeded", json.dumps(final["steps"], indent=1)[:2000]
    by_key = {step["key"]: step for step in final["steps"]}
    assert by_key["fetch_logs"]["status"] == "succeeded"
    assert by_key["classify"]["status"] == "succeeded"
    assert by_key["summarize"]["status"] == "succeeded"
    assert by_key["send"]["status"] == "succeeded"
    assert "Digest:" in (by_key["summarize"]["output"] or {}).get("summary", "")


def test_pack_failure_missing_connection_at_start(client, account, db_session, monkeypatch, embedded_worker):
    # incident-triage needs team-slack; without it the run is rejected outright.
    workflow = workflow_packs.install_pack(db_session, account["user"]["id"], "incident-triage")
    response = client.post(
        f"/api/v1/workflows/{workflow.id}/runs",
        json={"input": {"alert_text": "disk full", "alert_title": "disk", "runbook_url": "http://x/hook"}},
        headers=account["headers"],
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "connection_missing"
