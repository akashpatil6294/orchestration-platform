"""Phase 6 notification tests: channels and dispatch."""
from __future__ import annotations

import pytest


def test_channel_crud(client, account):
    # List events.
    events = client.get("/api/v1/notifications/events", headers=account["headers"]).json()
    assert "run.failed" in events["events"]

    # Create.
    created = client.post(
        "/api/v1/notifications/channels",
        json={"name": "ops", "url": "https://example.com/hook", "events": ["run.failed"]},
        headers=account["headers"],
    )
    assert created.status_code == 201, created.text
    channel_id = created.json()["id"]

    # List.
    listed = client.get("/api/v1/notifications/channels", headers=account["headers"]).json()
    assert any(c["id"] == channel_id for c in listed["items"])

    # Invalid event rejected.
    bad = client.post(
        "/api/v1/notifications/channels",
        json={"name": "x", "url": "https://example.com/hook", "events": ["nope"]},
        headers=account["headers"],
    )
    assert bad.status_code == 422

    # Non-HTTPS rejected.
    bad_url = client.post(
        "/api/v1/notifications/channels",
        json={"name": "x", "url": "http://example.com/hook", "events": ["run.failed"]},
        headers=account["headers"],
    )
    assert bad_url.status_code == 422

    # Delete.
    assert (
        client.delete(f"/api/v1/notifications/channels/{channel_id}", headers=account["headers"]).status_code
        == 204
    )


def test_dispatch_posts_signed_payload(client, account, db_session, monkeypatch):
    from app.services import notification_service

    created = client.post(
        "/api/v1/notifications/channels",
        json={"name": "ops", "url": "https://example.com/hook", "events": ["run.failed"]},
        headers=account["headers"],
    ).json()

    calls = []

    class FakeResponse:
        status_code = 200

    def fake_post(url, content=None, headers=None, timeout=None):
        calls.append({"url": url, "content": content, "headers": headers})
        return FakeResponse()

    monkeypatch.setattr("app.services.notification_service.httpx.post", fake_post)

    delivered = notification_service.dispatch_event(
        db_session,
        event="run.failed",
        owner_id=account["user"]["id"],
        payload={"run_id": "r1"},
    )
    assert delivered == 1
    assert len(calls) == 1
    assert calls[0]["url"] == "https://example.com/hook"
    assert b"run.failed" in calls[0]["content"]
    assert "X-Orchestrator-Signature" in calls[0]["headers"]

    # Unsubscribed event is not dispatched.
    delivered = notification_service.dispatch_event(
        db_session, event="run.succeeded", owner_id=account["user"]["id"], payload={}
    )
    assert delivered == 0
    assert len(calls) == 1


def test_dispatch_failures_do_not_raise(client, account, db_session, monkeypatch):
    from app.services import notification_service

    client.post(
        "/api/v1/notifications/channels",
        json={"name": "ops", "url": "https://example.com/hook", "events": ["run.failed"]},
        headers=account["headers"],
    )

    def boom(*args, **kwargs):
        raise ConnectionError("down")

    monkeypatch.setattr("app.services.notification_service.httpx.post", boom)
    # Should not raise.
    delivered = notification_service.dispatch_event(
        db_session, event="run.failed", owner_id=account["user"]["id"], payload={}
    )
    assert delivered == 0
