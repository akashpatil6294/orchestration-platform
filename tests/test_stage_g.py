"""Stage G: SSE run event streaming and server-side saved filters."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.database import SessionLocal
from app.models.event import RunEvent


@pytest.fixture()
def owned_run(client, account, workflow_factory):
    workflow_id = workflow_factory(account["headers"])["id"]
    response = client.post(
        f"/api/v1/workflows/{workflow_id}/runs", json={}, headers=account["headers"]
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _add_event(run_id: str, owner_id: str, message: str = "hello") -> int:
    db = SessionLocal()
    try:
        event = RunEvent(
            run_id=run_id,
            event_type="run.note",
            level="info",
            message=message,
            actor=owner_id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(event)
        db.commit()
        return event.seq
    finally:
        db.close()


def test_sse_replays_history(client, account, owned_run):
    _add_event(owned_run, account["user"]["id"], "first")
    _add_event(owned_run, account["user"]["id"], "second")
    response = client.get(f"/api/v1/runs/{owned_run}/events/stream", headers=account["headers"])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert "data:" in body
    assert "first" in body and "second" in body


def test_sse_after_seq_skips_old_events(client, account, owned_run):
    first_seq = _add_event(owned_run, account["user"]["id"], "old")
    _add_event(owned_run, account["user"]["id"], "new")
    response = client.get(
        f"/api/v1/runs/{owned_run}/events/stream?after_seq={first_seq}", headers=account["headers"]
    )
    assert response.status_code == 200
    body = response.text
    assert "new" in body
    assert "old" not in body


def test_sse_terminal_run_emits_done(client, account, owned_run):
    db = SessionLocal()
    try:
        from app.models.run import WorkflowRun

        run = db.get(WorkflowRun, owned_run)
        run.status = "succeeded"
        db.commit()
    finally:
        db.close()
    response = client.get(f"/api/v1/runs/{owned_run}/events/stream", headers=account["headers"])
    assert response.status_code == 200
    assert "event: done" in response.text


def test_sse_is_tenant_isolated(client, other_account, owned_run):
    response = client.get(
        f"/api/v1/runs/{owned_run}/events/stream", headers=other_account["headers"]
    )
    assert response.status_code in (403, 404)


def test_saved_filters_crud(client, account):
    headers = account["headers"]
    response = client.post(
        "/api/v1/saved-filters",
        json={"name": "Failures", "filters": {"status": "failed", "sort": "newest"}},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    filter_id = response.json()["id"]
    response = client.get("/api/v1/saved-filters", headers=headers)
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [filter_id]
    response = client.post(
        "/api/v1/saved-filters",
        json={"name": "Bad", "filters": {"bogus": "x"}},
        headers=headers,
    )
    assert response.status_code == 422
    response = client.delete(f"/api/v1/saved-filters/{filter_id}", headers=headers)
    assert response.status_code == 204
    response = client.get("/api/v1/saved-filters", headers=headers)
    assert response.json()["items"] == []


def test_saved_filters_are_per_user(client, account, other_account):
    response = client.post(
        "/api/v1/saved-filters", json={"name": "Mine", "filters": {}}, headers=account["headers"]
    )
    filter_id = response.json()["id"]
    response = client.get("/api/v1/saved-filters", headers=other_account["headers"])
    assert response.json()["items"] == []
    response = client.delete(f"/api/v1/saved-filters/{filter_id}", headers=other_account["headers"])
    assert response.status_code == 404
