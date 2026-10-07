"""Phase-one orchestration behaviours through the real API and worker protocol."""
from __future__ import annotations

from typing import Any

from app.worker import registry


class _Context:
    def __init__(self, task: dict[str, Any]) -> None:
        self.task = task
        self.idempotency_key = task["idempotency_key"]
        self.attempt = task["attempt"]
        self.run_id = task["run_id"]
        self.step_key = task["step_key"]
        self.deadline_at = task.get("deadline_at")

    def log(self, _message: str, **_fields: Any) -> None:
        pass

    def cancelled(self) -> bool:
        return False


def _report_task(worker_client, task: dict[str, Any]) -> None:
    client = worker_client["client"]
    try:
        handler = registry.resolve(task["type"])
        output = handler.func(task.get("input") or {}, _Context(task))
    except Exception as exc:  # worker protocol reports handler errors to the API
        response = client.post(
            f"/api/v1/tasks/{task['id']}/fail",
            json={
                "worker_id": worker_client["worker_id"],
                "lease_token": task["lease_token"],
                "error": {"type": type(exc).__name__, "message": str(exc)},
                "retryable": getattr(exc, "retryable", True),
            },
        )
    else:
        response = client.post(
            f"/api/v1/tasks/{task['id']}/complete",
            json={"worker_id": worker_client["worker_id"], "lease_token": task["lease_token"], "output": output},
        )
    assert response.status_code == 200, response.text


def _claim_for_run(worker_client, run_id: str) -> dict[str, Any] | None:
    client = worker_client["client"]
    for _ in range(40):
        response = client.post(
            "/api/v1/workers/claim",
            json={"worker_id": worker_client["worker_id"], "available_slots": 4, "task_types": registry.types()},
        )
        assert response.status_code == 200, response.text
        tasks = response.json().get("tasks") or []
        if not tasks:
            return None
        selected = None
        for task in tasks:
            if task["run_id"] == run_id and selected is None:
                selected = task
            else:
                _report_task(worker_client, task)
        if selected:
            return selected
    return None


def _detail(client, account, run_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/runs/{run_id}", headers=account["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def test_condition_branches_and_all_success_join(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        {
            "name": "Conditional paths",
            "steps": [
                {"id": "fast", "type": "demo.echo", "input": {"value": "fast"}, "when": "input.route == 'fast'"},
                {"id": "slow", "type": "demo.echo", "input": {"value": "slow"}, "when": "input.route == 'slow'"},
                {"id": "join", "type": "demo.summarize", "input": {"title": "joined"}, "depends_on": ["fast", "slow"]},
            ],
        },
    )
    started = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {"route": "fast"}}, headers=account["headers"])
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    states = {step["key"]: step["status"] for step in _detail(client, account, run_id)["steps"]}
    assert states == {"fast": "pending", "slow": "skipped", "join": "pending"}

    first = _claim_for_run(worker_client, run_id)
    assert first and first["step_key"] == "fast"
    _report_task(worker_client, first)
    joined = _claim_for_run(worker_client, run_id)
    assert joined and joined["step_key"] == "join"
    _report_task(worker_client, joined)
    assert _detail(client, account, run_id)["status"] == "succeeded"


def test_condition_node_selects_if_else_branch(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        {
            "name": "If else routes",
            "steps": [
                {"id": "route", "type": "condition", "when": "input.amount > 100", "if_true": "review", "if_false": "auto", "input": {}},
                {"id": "review", "type": "demo.echo", "input": {"value": "review"}, "depends_on": ["route"]},
                {"id": "auto", "type": "demo.echo", "input": {"value": "auto"}, "depends_on": ["route"]},
            ],
        },
    )
    started = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {"amount": 150}}, headers=account["headers"])
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    states = {step["key"]: step["status"] for step in _detail(client, account, run_id)["steps"]}
    assert states == {"route": "succeeded", "review": "pending", "auto": "skipped"}
    task = _claim_for_run(worker_client, run_id)
    assert task and task["step_key"] == "review"
    _report_task(worker_client, task)
    assert _detail(client, account, run_id)["status"] == "succeeded"


def test_fanout_creates_ordered_children_and_enforces_max_concurrency(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        {
            "name": "Batch fanout",
            "default_max_parallel": 4,
            "steps": [
                {"id": "source", "type": "demo.echo", "input": {"value": [{"v": "one"}, {"v": "two"}, {"v": "three"}]}},
                {
                    "id": "map",
                    "type": "demo.echo",
                    "input": {"value": "{{item.v}}", "index": "{{index}}"},
                    "depends_on": ["source"],
                    "foreach": "{{steps.source.output.value}}",
                    "max_concurrency": 1,
                    "partial_failure": "continue",
                },
                {"id": "collect", "type": "demo.summarize", "input": {"title": "batch"}, "depends_on": ["map"]},
            ],
        },
    )
    started = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"])
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    source = _claim_for_run(worker_client, run_id)
    assert source and source["step_key"] == "source"
    _report_task(worker_client, source)

    detail = _detail(client, account, run_id)
    parent = next(step for step in detail["steps"] if step["key"] == "map")
    assert parent["status"] == "waiting_children"
    children = sorted((step for step in detail["steps"] if step.get("parent_step_id") == parent["id"]), key=lambda item: item["foreach_index"])
    assert len(children) == 3
    assert [step["key"] for step in children] == ["map__i0", "map__i1", "map__i2"]

    for expected_index, expected_value in enumerate(("one", "two", "three")):
        child = _claim_for_run(worker_client, run_id)
        assert child and child["step_key"] == f"map__i{expected_index}"
        assert child["input"] == {"value": expected_value, "index": expected_index}
        _report_task(worker_client, child)
    collect = _claim_for_run(worker_client, run_id)
    assert collect and collect["step_key"] == "collect"
    _report_task(worker_client, collect)
    detail = _detail(client, account, run_id)
    assert detail["status"] == "succeeded"
    map_output = next(step["output"] for step in detail["steps"] if step["key"] == "map")
    assert [item["value"] for item in map_output] == ["one", "two", "three"]


def test_approval_gate_is_audited_and_owner_scoped(client, account, other_account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        {
            "name": "Approval gate",
            "steps": [
                {"id": "approve", "type": "approval", "input": {}, "approvers": [account["email"]]},
                {"id": "publish", "type": "demo.echo", "input": {"value": "published"}, "depends_on": ["approve"]},
            ],
        },
    )
    started = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"])
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    assert next(step for step in _detail(client, account, run_id)["steps"] if step["key"] == "approve")["status"] == "waiting_approval"

    denied = client.post(f"/api/v1/runs/{run_id}/steps/approve/approval", json={"decision": "approve"}, headers=other_account["headers"])
    assert denied.status_code == 404
    approved = client.post(
        f"/api/v1/runs/{run_id}/steps/approve/approval",
        json={"decision": "approve", "comment": "Reviewed"},
        headers=account["headers"],
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "succeeded"
    events = client.get(f"/api/v1/runs/{run_id}/events", headers=account["headers"]).json()["items"]
    assert any(event["type"] == "approval.approved" and event["payload"]["comment"] == "Reviewed" for event in events)
    task = _claim_for_run(worker_client, run_id)
    assert task and task["step_key"] == "publish"
    _report_task(worker_client, task)
    assert _detail(client, account, run_id)["status"] == "succeeded"


def test_pause_resume_controls_claims(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(account["headers"], {"name": "Pause control", "steps": [{"id": "work", "type": "demo.echo", "input": {"value": 1}}]})
    started = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"])
    run_id = started.json()["id"]
    paused = client.post(f"/api/v1/runs/{run_id}/pause", headers=account["headers"])
    assert paused.status_code == 200 and paused.json()["status"] == "paused"

    claimed = worker_client["client"].post(
        "/api/v1/workers/claim",
        json={"worker_id": worker_client["worker_id"], "available_slots": 4, "task_types": registry.types()},
    )
    assert all(task["run_id"] != run_id for task in claimed.json().get("tasks", []))
    resumed = client.post(f"/api/v1/runs/{run_id}/resume", headers=account["headers"])
    assert resumed.status_code == 200
    task = _claim_for_run(worker_client, run_id)
    assert task and task["step_key"] == "work"
    _report_task(worker_client, task)
    assert _detail(client, account, run_id)["status"] == "succeeded"


def test_cached_step_reuses_result_between_runs(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        {"name": "Cached computation", "steps": [{"id": "compute", "type": "demo.echo", "input": {"value": "same"}, "cache": {"ttl_seconds": 60}}]},
    )
    first = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    task = _claim_for_run(worker_client, first["id"])
    assert task and task["step_key"] == "compute"
    _report_task(worker_client, task)
    assert _detail(client, account, first["id"])["status"] == "succeeded"

    second = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    detail = _detail(client, account, second["id"])
    assert detail["status"] == "succeeded"
    step = detail["steps"][0]
    assert step["attempts"] == 0
    assert step["output"]["value"] == "same"
    events = client.get(f"/api/v1/runs/{second['id']}/events", headers=account["headers"]).json()["items"]
    assert any(event["type"] == "step.cache_hit" for event in events)


def test_large_step_output_is_stored_as_owner_scoped_artifact(
    client, account, other_account, worker_client, workflow_factory, monkeypatch, tmp_path
):
    from app.config import settings
    from app.services import artifact_service, worker_service

    monkeypatch.setattr(settings, "artifact_dir", str(tmp_path / "artifacts"))
    monkeypatch.setattr(settings, "max_request_bytes", 3_000_000)
    monkeypatch.setattr(settings, "max_inline_output_bytes", 1_048_576)
    monkeypatch.setattr(settings, "max_task_output_bytes", 2_000_000)
    monkeypatch.setattr(artifact_service.settings, "max_inline_output_bytes", 1_048_576)
    monkeypatch.setattr(artifact_service.settings, "max_task_output_bytes", 2_000_000)
    monkeypatch.setattr(worker_service.settings, "max_inline_output_bytes", 1_048_576)
    monkeypatch.setattr(worker_service.settings, "max_task_output_bytes", 2_000_000)
    workflow = workflow_factory(
        account["headers"],
        {"name": "Large output", "steps": [{"id": "payload", "type": "demo.echo", "input": {}}]},
    )
    started = client.post(
        f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]
    )
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    task = _claim_for_run(worker_client, run_id)
    assert task and task["step_key"] == "payload"
    output = {"text": "x" * 1_100_000}
    completed = worker_client["client"].post(
        f"/api/v1/tasks/{task['id']}/complete",
        json={"worker_id": worker_client["worker_id"], "lease_token": task["lease_token"], "output": output},
    )
    assert completed.status_code == 200, completed.text

    detail = _detail(client, account, run_id)
    pointer = detail["steps"][0]["output"]
    assert "artifact_id" in pointer, f"settings.inline={settings.max_inline_output_bytes}, output_keys={list(pointer)}"
    downloaded = client.get(pointer["artifact_uri"], headers=account["headers"])
    assert downloaded.status_code == 200
    assert downloaded.json() == output
    forbidden = client.get(pointer["artifact_uri"], headers=other_account["headers"])
    assert forbidden.status_code == 404


def test_demo_seed_installs_phase1_samples_idempotently(db_session, account):
    from app.services.demo_data import install_demo_workflows

    first = install_demo_workflows(db_session, account["user"]["id"])
    db_session.commit()
    second = install_demo_workflows(db_session, account["user"]["id"])
    db_session.commit()
    assert len(first) == 8
    assert len(second) == 8
    assert all(workflow.latest_version == 1 for workflow in second)
    assert {workflow.name for workflow in second} >= {
        "Daily report pipeline",
        "Order routing with conditions",
        "PDF batch extraction",
        "Production deploy approval",
        "Booking saga with rollback",
        "Webhook order intake",
        "Queue priorities and dead letters",
        "Invoice extraction with approval",
    }


def test_subworkflow_waits_returns_output_and_rejects_cycles(client, account, worker_client, workflow_factory):
    child = workflow_factory(
        account["headers"],
        {"name": "Reusable child", "steps": [{"id": "echo", "type": "demo.echo", "input": {"value": "{{input.customer}}"}}]},
    )
    parent = workflow_factory(
        account["headers"],
        {
            "name": "Parent",
            "steps": [{"id": "child", "type": "workflow.run", "input": {"workflow": child["id"], "input": {"customer": "{{input.customer}}"}}}],
        },
    )
    started = client.post(f"/api/v1/workflows/{parent['id']}/runs", json={"input": {"customer": "Ada"}}, headers=account["headers"])
    assert started.status_code == 201, started.text
    parent_id = started.json()["id"]
    detail = _detail(client, account, parent_id)
    parent_step = next(step for step in detail["steps"] if step["key"] == "child")
    assert parent_step["status"] == "waiting_subworkflow"
    from app.database import SessionLocal
    from app.models.run import StepRun

    with SessionLocal() as db:
        row = db.get(StepRun, parent_step["id"])
        child_run_id = row.child_run_id
    child_task = _claim_for_run(worker_client, child_run_id)
    assert child_task and child_task["input"]["value"] == "Ada"
    _report_task(worker_client, child_task)
    detail = _detail(client, account, parent_id)
    assert detail["status"] == "succeeded"
    assert next(step for step in detail["steps"] if step["key"] == "child")["output"]["echo"]["value"] == "Ada"

    cyclic = workflow_factory(
        account["headers"],
        {"name": "Cycle", "steps": [{"id": "again", "type": "workflow.run", "input": {"workflow": "placeholder"}}]},
        publish=False,
    )
    client.patch(
        f"/api/v1/workflows/{cyclic['id']}",
        json={"steps": [{"id": "again", "type": "workflow.run", "input": {"workflow": cyclic["id"]}}]},
        headers=account["headers"],
    )
    client.post(f"/api/v1/workflows/{cyclic['id']}/publish", json={}, headers=account["headers"])
    cyclic_run = client.post(f"/api/v1/workflows/{cyclic['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    assert _detail(client, account, cyclic_run["id"])["status"] == "failed"


def test_saga_compensates_successful_work_before_on_failure(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        {
            "name": "Booking saga",
            "steps": [
                {"id": "reserve", "type": "demo.echo", "input": {"value": "reserved"}, "compensate": "release"},
                {"id": "charge", "type": "demo.fail", "input": {"message": "card rejected"}, "depends_on": ["reserve"]},
                {"id": "release", "type": "demo.echo", "input": {"value": "{{steps.reserve.output.value}}"}, "depends_on": ["reserve"]},
            ],
            "on_failure": [{"id": "alert", "type": "demo.echo", "input": {"value": "rollback finished"}}],
        },
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    for _ in range(10):
        task = _claim_for_run(worker_client, run["id"])
        if task is None:
            break
        _report_task(worker_client, task)
        if _detail(client, account, run["id"])["status"] == "failed":
            break
    detail = _detail(client, account, run["id"])
    assert detail["status"] == "failed"
    statuses = {step["key"]: step["status"] for step in detail["steps"]}
    assert statuses["reserve"] == "succeeded"
    assert statuses["charge"] == "failed"
    assert statuses["release"] == "succeeded"
    assert statuses["alert"] == "succeeded"
    events = client.get(f"/api/v1/runs/{run['id']}/events", headers=account["headers"]).json()["items"]
    completed = {event["step_key"]: event["seq"] for event in events if event["type"] == "step.succeeded"}
    assert completed["release"] < completed["alert"]
