"""Phase 3B backend support: dashboard attention queue, chart series, runs list
sort and date-range filters.

These tests cover the additive dashboard payload (``needs_attention``,
``runs_timeline``, ``duration_trend``) and the extended ``GET /api/v1/runs``
query surface the runs page is built on.
"""
from __future__ import annotations


def _fail_definition(name="3B failing run"):
    return {
        "name": name,
        "steps": [
            {"id": "bad", "type": "demo.fail", "input": {}, "depends_on": [], "retries": 0, "timeout_seconds": 60},
        ],
    }


def _approval_definition(name="3B approval run"):
    return {
        "name": name,
        "steps": [
            {"id": "approve", "type": "approval", "input": {}, "approvers": [], "depends_on": [], "timeout_seconds": 3600},
        ],
    }


def _echo_definition(name="3B echo run"):
    return {
        "name": name,
        "steps": [
            {"id": "ok", "type": "demo.echo", "input": {"value": 1}, "depends_on": [], "timeout_seconds": 60},
        ],
    }


def _start_and_drain_failure(client, account, worker_client, workflow):
    started = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"])
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    for _ in range(30):
        claimed = worker_client["client"].post(
            "/api/v1/workers/claim",
            json={"worker_id": worker_client["worker_id"], "available_slots": 4, "task_types": ["demo.fail"]},
        )
        tasks = claimed.json().get("tasks") or []
        if not tasks:
            detail = client.get(f"/api/v1/runs/{run_id}", headers=account["headers"]).json()
            if detail["status"] == "failed":
                break
            continue
        for task in tasks:
            worker_client["client"].post(
                f"/api/v1/tasks/{task['id']}/fail",
                json={
                    "worker_id": worker_client["worker_id"],
                    "lease_token": task["lease_token"],
                    "error": {"type": "RuntimeError", "message": "boom"},
                    "retryable": False,
                },
            )
    detail = client.get(f"/api/v1/runs/{run_id}", headers=account["headers"]).json()
    assert detail["status"] == "failed", detail["status"]
    return run_id


def test_dashboard_attention_timeline_and_trend(client, account, worker_client, workflow_factory, db_session):
    failed_workflow = workflow_factory(account["headers"], _fail_definition("3B failed workflow"))
    failed_run_id = _start_and_drain_failure(client, account, worker_client, failed_workflow)

    approval_workflow = workflow_factory(account["headers"], _approval_definition("3B approval workflow"))
    approval_run_id = client.post(
        f"/api/v1/workflows/{approval_workflow['id']}/runs", json={"input": {}}, headers=account["headers"]
    ).json()["id"]

    dashboard = client.get("/api/v1/runs/dashboard", headers=account["headers"])
    assert dashboard.status_code == 200, dashboard.text
    payload = dashboard.json()

    kinds = {item["kind"] for item in payload["needs_attention"]}
    assert "run_failed" in kinds
    assert "dlq" in kinds
    assert "approval" in kinds

    failed_item = next(item for item in payload["needs_attention"] if item["kind"] == "run_failed")
    assert failed_item["run_id"] == failed_run_id
    assert failed_item["severity"] == "bad"

    approval_item = next(item for item in payload["needs_attention"] if item["kind"] == "approval")
    assert approval_item["run_id"] == approval_run_id
    assert "approve" in approval_item["detail"]

    timeline = payload["runs_timeline"]
    assert 1 <= len(timeline) <= 24
    assert sum(bucket["started"] for bucket in timeline) >= 2
    assert sum(bucket["failed"] for bucket in timeline) >= 1
    trend = payload["duration_trend"]
    assert len(trend) == len(timeline)
    assert any(bucket["avg_duration_seconds"] is not None for bucket in trend)


def test_dashboard_window_one_hour_single_bucket(client, account):
    response = client.get("/api/v1/runs/dashboard", params={"window_hours": 1}, headers=account["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["runs_timeline"]) == 1
    assert len(body["duration_trend"]) == 1


def test_dashboard_window_bounds_rejected(client, account):
    assert client.get("/api/v1/runs/dashboard", params={"window_hours": 0}, headers=account["headers"]).status_code == 422
    assert client.get("/api/v1/runs/dashboard", params={"window_hours": 721}, headers=account["headers"]).status_code == 422


def test_dashboard_attention_is_owner_scoped(client, account, other_account, worker_client, workflow_factory):
    workflow = workflow_factory(account["headers"], _fail_definition("3B tenant isolation"))
    _start_and_drain_failure(client, account, worker_client, workflow)

    other = client.get("/api/v1/runs/dashboard", headers=other_account["headers"]).json()
    assert other["needs_attention"] == []
    assert all(bucket["started"] == 0 for bucket in other["runs_timeline"])
    assert other["stats"]["runs_total"] == 0


def test_runs_list_sort_and_created_before(client, account, other_account, worker_client, workflow_factory, db_session):
    succeeded_workflow = workflow_factory(account["headers"], _echo_definition("3B sort succeeded"))
    failed_workflow = workflow_factory(account["headers"], _fail_definition("3B sort failed"))

    started_ok = client.post(f"/api/v1/workflows/{succeeded_workflow['id']}/runs", json={"input": {}}, headers=account["headers"])
    ok_run_id = started_ok.json()["id"]
    for _ in range(30):
        claimed = worker_client["client"].post(
            "/api/v1/workers/claim",
            json={"worker_id": worker_client["worker_id"], "available_slots": 4, "task_types": ["demo.echo"]},
        )
        tasks = claimed.json().get("tasks") or []
        if not tasks:
            break
        for task in tasks:
            worker_client["client"].post(
                f"/api/v1/tasks/{task['id']}/complete",
                json={
                    "worker_id": worker_client["worker_id"],
                    "lease_token": task["lease_token"],
                    "output": {"value": task["input"].get("value")},
                },
            )
    assert client.get(f"/api/v1/runs/{ok_run_id}", headers=account["headers"]).json()["status"] == "succeeded"

    failed_run_id = _start_and_drain_failure(client, account, worker_client, failed_workflow)

    # Pin deterministic durations: wall-clock timing ties under suite load,
    # which made this sort assertion flaky. The sort logic itself is unchanged.
    from datetime import timedelta

    from app.models.run import WorkflowRun

    ok_run = db_session.get(WorkflowRun, ok_run_id)
    failed_run = db_session.get(WorkflowRun, failed_run_id)
    ok_run.finished_at = ok_run.started_at + timedelta(seconds=10)
    failed_run.finished_at = failed_run.started_at + timedelta(seconds=1)
    db_session.commit()

    headers = account["headers"]

    longest = client.get("/api/v1/runs", params={"sort": "longest"}, headers=headers).json()
    assert [item["id"] for item in longest["items"]] == [ok_run_id, failed_run_id], longest["items"]

    oldest = client.get("/api/v1/runs", params={"sort": "oldest"}, headers=headers).json()
    newest = client.get("/api/v1/runs", params={"sort": "newest"}, headers=headers).json()
    if oldest["items"][0]["created_at"] != newest["items"][0]["created_at"]:
        assert oldest["items"][0]["id"] == ok_run_id
        assert newest["items"][0]["id"] == failed_run_id

    by_status = client.get("/api/v1/runs", params={"sort": "status"}, headers=headers).json()
    statuses = [item["status"] for item in by_status["items"]]
    assert statuses == sorted(statuses)

    epoch = client.get("/api/v1/runs", params={"created_before": "2000-01-01T00:00:00Z"}, headers=headers).json()
    assert epoch["total"] == 0
    recent = client.get("/api/v1/runs", params={"created_before": "2999-01-01T00:00:00Z"}, headers=headers).json()
    assert recent["total"] == 2

    other = client.get("/api/v1/runs", params={"sort": "longest"}, headers=other_account["headers"]).json()
    assert other["total"] == 0


def test_runs_list_rejects_unknown_sort(client, account):
    response = client.get("/api/v1/runs", params={"sort": "bogus"}, headers=account["headers"])
    assert response.status_code == 422


def test_demo_install_is_idempotent_and_owner_scoped(client, account, other_account):
    first = client.post("/api/v1/demo/install", headers=account["headers"])
    assert first.status_code == 200, first.text
    names = {item["name"] for item in first.json()["installed"]}
    assert len(names) > 1

    second = client.post("/api/v1/demo/install", headers=account["headers"])
    assert second.status_code == 200
    assert {item["name"] for item in second.json()["installed"]} == names

    mine = client.get("/api/v1/workflows", headers=account["headers"]).json()
    theirs = client.get("/api/v1/workflows", headers=other_account["headers"]).json()
    assert theirs["total"] == 0
    assert mine["total"] >= len(names)
