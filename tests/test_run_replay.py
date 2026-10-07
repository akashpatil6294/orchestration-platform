"""Run replay with edited input (Stage H, H3).

POST /runs/{id}/replay creates a brand-new run linked to the original via
parent_run_id, using the original's version with replacement input. The
original run is untouched.
"""
from __future__ import annotations


def _create_and_publish(client, headers):
    wf = client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Replay workflow",
            "description": "",
            "steps": [{"id": "a", "type": "demo.echo", "input": {"value": "hi"}}],
        },
    )
    assert wf.status_code == 201, wf.text
    workflow_id = wf.json()["id"]
    pub = client.post(f"/api/v1/workflows/{workflow_id}/publish", headers=headers, json={"note": "v1"})
    assert pub.status_code == 200, pub.text
    run = client.post(f"/api/v1/workflows/{workflow_id}/runs", headers=headers, json={"input": {"x": 1}})
    assert run.status_code == 201, run.text
    return workflow_id, run.json()


def test_replay_creates_linked_run_with_edited_input(client, account):
    workflow_id, original = _create_and_publish(client, account["headers"])
    response = client.post(
        f"/api/v1/runs/{original['id']}/replay",
        headers=account["headers"],
        json={"input": {"x": 2, "extra": "new"}},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"] != original["id"]
    assert body["parent_run_id"] == original["id"]
    assert body["trigger"] == "replay"
    assert body["version"] == original["version"]
    # The new run carries the edited input.
    detail = client.get(f"/api/v1/runs/{body['id']}", headers=account["headers"])
    assert detail.status_code == 200, detail.text
    assert detail.json()["input"] == {"x": 2, "extra": "new"}


def test_replay_leaves_original_untouched(client, account):
    _, original = _create_and_publish(client, account["headers"])
    replayed = client.post(
        f"/api/v1/runs/{original['id']}/replay",
        headers=account["headers"],
        json={"input": {"x": 99}},
    )
    assert replayed.status_code == 201, replayed.text
    detail = client.get(f"/api/v1/runs/{original['id']}", headers=account["headers"])
    assert detail.status_code == 200
    assert detail.json()["input"] == {"x": 1}


def test_replay_other_users_run_404s(client, account, other_account):
    _, original = _create_and_publish(client, account["headers"])
    response = client.post(
        f"/api/v1/runs/{original['id']}/replay",
        headers=other_account["headers"],
        json={"input": {}},
    )
    assert response.status_code == 404, response.text


def test_attempt_view_carries_error_class(app_module, client, account):
    """attempt_view exposes retryable vs permanent error classes."""
    from app.services import run_service

    class FakeAttempt:
        id = "att1"
        attempt_no = 1
        worker_id = "w1"
        status = "failed"
        started_at = None
        finished_at = None
        output_data = None
        logs = []
        error = {"message": "boom", "retryable": True}

    view = run_service.attempt_view(FakeAttempt())
    assert view["error_class"] == "retryable"
    assert view["error_message"] == "boom"

    class PermanentAttempt(FakeAttempt):
        error = {"message": "bad input", "retryable": False}

    view = run_service.attempt_view(PermanentAttempt())
    assert view["error_class"] == "permanent"
