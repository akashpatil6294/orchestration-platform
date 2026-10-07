"""Builder test runs (Stage H, H1).

POST /workflows/{id}/test-run executes the current draft without publishing.
Test runs are marked is_test: no triggers, no notifications, no production
quotas. A single step can be tested in isolation with pinned sample outputs.
"""
from __future__ import annotations


def _create_workflow(client, headers):
    response = client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Test-run workflow",
            "description": "",
            "steps": [
                {"id": "a", "type": "demo.echo", "input": {"value": "hello"}},
                {"id": "b", "type": "demo.echo", "input": {"value": "world"}, "depends_on": ["a"]},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_test_run_executes_draft_without_publishing(client, account):
    workflow = _create_workflow(client, account["headers"])
    response = client.post(
        f"/api/v1/workflows/{workflow['id']}/test-run",
        headers=account["headers"],
        json={"input": {"foo": "bar"}},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["is_test"] is True
    assert body["trigger"] == "test"
    assert body["version"] == 0  # draft, not a published version
    assert body["total_steps"] == 2


def test_test_run_skips_quota_check(client, account, monkeypatch):
    """Test runs never consume production quotas, even when quota is exhausted."""
    from app.services import quota_service

    def _boom(db, triggered_by):
        raise AssertionError("quota must not be checked for test runs")

    monkeypatch.setattr(quota_service, "check_run_quota", _boom)
    workflow = _create_workflow(client, account["headers"])
    response = client.post(
        f"/api/v1/workflows/{workflow['id']}/test-run",
        headers=account["headers"],
        json={},
    )
    assert response.status_code == 201, response.text


def test_single_step_test_runs_only_that_step(client, account):
    workflow = _create_workflow(client, account["headers"])
    response = client.post(
        f"/api/v1/workflows/{workflow['id']}/test-run",
        headers=account["headers"],
        json={"step_id": "b", "pinned_outputs": {"a": {"echoed": "sample"}}},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["is_test"] is True
    assert body["total_steps"] == 1


def test_single_step_test_unknown_step_404s(client, account):
    workflow = _create_workflow(client, account["headers"])
    response = client.post(
        f"/api/v1/workflows/{workflow['id']}/test-run",
        headers=account["headers"],
        json={"step_id": "nope"},
    )
    assert response.status_code == 404, response.text


def test_test_run_does_not_fire_triggers_or_notifications(client, account, monkeypatch):
    """A test run reaching a terminal state stays silent."""
    from app.services import notification_service, run_service
    from app.services import trigger_service

    calls: list[str] = []

    def _notify(*args, **kwargs):
        calls.append("notify")

    def _fire(db, run):
        calls.append("trigger")

    monkeypatch.setattr(notification_service, "dispatch_event", _notify)
    monkeypatch.setattr(trigger_service, "fire_workflow_success_triggers", _fire)

    workflow = _create_workflow(client, account["headers"])
    response = client.post(
        f"/api/v1/workflows/{workflow['id']}/test-run",
        headers=account["headers"],
        json={"step_id": "a"},
    )
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]

    # Drive the run to a terminal state through the service layer.
    from app.database import SessionLocal

    with SessionLocal() as db:
        run = db.get(run_service.WorkflowRun, run_id)
        # Simulate completion of the single step.
        steps = run_service.steps_for(db, run.id)
        assert len(steps) == 1
        steps[0].status = "succeeded"
        steps[0].finished_at = run_service.utcnow()
        db.commit()
        run_service._finish_run(db, run, status="succeeded")
        db.commit()

    assert calls == [], f"test run fired side effects: {calls}"
