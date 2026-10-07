"""Stage F: Phase 6 gaps — channel types, delivery log, expanded metrics."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


class _HookHandler(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        _HookHandler.received.append(self.rfile.read(length))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def hook_server():
    _HookHandler.received = []
    server = HTTPServer(("127.0.0.1", 0), _HookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hook"
    server.shutdown()


def _create_channel(client, headers, **kwargs):
    payload = {"name": "ch", "url": "https://example.com/hook", "events": ["run.failed"]}
    payload.update(kwargs)
    return client.post("/api/v1/notifications/channels", json=payload, headers=headers)


def test_channel_types_validated(client, account):
    # Bad slack URL rejected.
    bad_slack = _create_channel(
        client, account["headers"], channel_type="slack", url="https://evil.example.com/hook"
    )
    assert bad_slack.status_code == 422
    # Good slack URL accepted.
    good_slack = _create_channel(
        client,
        account["headers"],
        channel_type="slack",
        url="https://hooks.slack.com/services/T/B/X",
    )
    assert good_slack.status_code == 201, good_slack.text
    assert good_slack.json()["channel_type"] == "slack"
    # Bad email rejected; good email accepted.
    bad_email = _create_channel(client, account["headers"], channel_type="email", url="not-an-address")
    assert bad_email.status_code == 422
    good_email = _create_channel(
        client, account["headers"], channel_type="email", url="ops@example.com"
    )
    assert good_email.status_code == 201
    assert good_email.json()["channel_type"] == "email"
    # Unknown type rejected.
    unknown = _create_channel(client, account["headers"], channel_type="pager")
    assert unknown.status_code == 422


def test_webhook_dispatch_records_delivery(client, account, db_session, hook_server):
    from app.services import notification_service

    created = _create_channel(client, account["headers"], url=hook_server)
    assert created.status_code == 201, created.text

    delivered = notification_service.dispatch_event(
        db_session,
        event="run.failed",
        owner_id=account["user"]["id"],
        payload={"workflow_name": "w", "run_id": "r", "status": "failed"},
    )
    db_session.commit()
    assert delivered == 1
    assert len(_HookHandler.received) == 1

    deliveries = client.get("/api/v1/notifications/deliveries", headers=account["headers"]).json()
    assert len(deliveries["items"]) == 1
    item = deliveries["items"][0]
    assert item["event"] == "run.failed"
    assert item["status"] == "delivered"
    assert item["status_code"] == 200


def test_email_dispatch_without_smtp_records_failure(client, account, db_session, monkeypatch):
    from app.config import settings
    from app.services import notification_service

    monkeypatch.setattr(settings, "smtp_host", "")
    created = _create_channel(client, account["headers"], channel_type="email", url="ops@example.com")
    assert created.status_code == 201

    delivered = notification_service.dispatch_event(
        db_session,
        event="run.failed",
        owner_id=account["user"]["id"],
        payload={"workflow_name": "w", "run_id": "r", "status": "failed"},
    )
    db_session.commit()
    assert delivered == 0

    deliveries = client.get("/api/v1/notifications/deliveries", headers=account["headers"]).json()
    assert deliveries["items"][0]["status"] == "failed"
    assert "SMTP" in (deliveries["items"][0]["error"] or "")


def test_metrics_include_new_series(client, account, workflow_factory):
    from app.core.metrics import counter, observe, render_prometheus
    from app.services import demo_data

    workflow = workflow_factory(account["headers"], demo_data.DEMO_SMOKE_DEFINITION)
    started = client.post(
        f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]
    )
    assert started.status_code == 201
    body = client.get("/metrics").text
    # Gauges refresh on every scrape; the run creation bumps the tenant counter.
    for name in ("orchestrator_queue_depth", "orchestrator_dlq_size", "orchestrator_tenant_runs_created_total"):
        assert name in body, name
    # Counters/histograms render once observed — verify their exposition format.
    counter("orchestrator_lease_expirations_total")
    observe("orchestrator_claim_seconds", 0.01, {"queue": "default"})
    counter(
        "orchestrator_notification_deliveries_total",
        {"channel_type": "webhook", "event": "run.failed", "status": "delivered"},
    )
    rendered = render_prometheus()
    for name in (
        "orchestrator_claim_seconds",
        "orchestrator_lease_expirations_total",
        "orchestrator_notification_deliveries_total",
    ):
        assert name in rendered, name


def test_queue_depth_gauge_reflects_pending_steps(client, account, workflow_factory):
    from app.services import demo_data

    workflow = workflow_factory(account["headers"], demo_data.DEMO_SMOKE_DEFINITION)
    started = client.post(
        f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]
    )
    assert started.status_code == 201
    body = client.get("/metrics").text
    assert "orchestrator_queue_depth" in body


def test_channel_workflow_filter(client, account, db_session, workflow_factory, hook_server):
    from app.services import notification_service
    from app.services import demo_data

    workflow = workflow_factory(account["headers"], demo_data.DEMO_SMOKE_DEFINITION)
    other = workflow_factory(account["headers"], demo_data.DEMO_SMOKE_DEFINITION, name="other")

    # Channel scoped to `workflow` only.
    created = client.post(
        "/api/v1/notifications/channels",
        json={
            "name": "scoped",
            "url": hook_server,
            "events": ["run.failed"],
            "workflow_id": workflow["id"],
        },
        headers=account["headers"],
    )
    assert created.status_code == 201, created.text
    assert created.json()["workflow_id"] == workflow["id"]

    _HookHandler.received = []
    notification_service.dispatch_event(
        db_session, event="run.failed", owner_id=account["user"]["id"],
        payload={}, workflow_id=other["id"],
    )
    assert len(_HookHandler.received) == 0
    notification_service.dispatch_event(
        db_session, event="run.failed", owner_id=account["user"]["id"],
        payload={}, workflow_id=workflow["id"],
    )
    assert len(_HookHandler.received) == 1
    db_session.commit()
