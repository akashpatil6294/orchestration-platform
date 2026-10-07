"""Execution tests: claiming, dependency gating, retries, leases, cancellation.

These drive the real worker protocol through the API so the behaviour a reviewer
sees in the UI is exactly what is asserted here.
"""
from __future__ import annotations

import pytest


def drain(worker_client, *, max_rounds=60, run_id=None):
    """Claim and execute tasks until nothing is claimable, mimicking a worker.

    Workers form a shared fleet, so work left over from an earlier test may still
    be claimable. Everything claimed is executed (otherwise the fleet would stall),
    but ``results`` reports only the tasks belonging to ``run_id`` so each test
    asserts on its own sequence.
    """
    from app.worker.registry import registry

    client = worker_client["client"]
    results = []
    for _ in range(max_rounds):
        claimed = client.post(
            "/api/v1/workers/claim",
            json={"worker_id": worker_client["worker_id"], "available_slots": 4, "task_types": registry.types()},
        )
        assert claimed.status_code == 200, claimed.text
        tasks = claimed.json().get("tasks") or []
        if not tasks:
            break
        for task in tasks:
            mine = run_id is None or task["run_id"] == run_id
            handler = registry.resolve(task["type"])
            try:
                output = handler.func(task["input"], _Context(task))
                response = client.post(
                    f"/api/v1/tasks/{task['id']}/complete",
                    json={"worker_id": worker_client["worker_id"], "lease_token": task["lease_token"], "output": output},
                )
                if mine:
                    results.append((task, "completed", response))
            except Exception as exc:
                response = client.post(
                    f"/api/v1/tasks/{task['id']}/fail",
                    json={
                        "worker_id": worker_client["worker_id"],
                        "lease_token": task["lease_token"],
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                        "retryable": getattr(exc, "retryable", True),
                    },
                )
                if mine:
                    results.append((task, "failed", response))
    return results


def claim_for_run(worker_client, run_id, *, slots=1, task_types=None):
    """Claim tasks until one belongs to ``run_id`` (workers are a shared fleet)."""
    from app.worker.registry import registry

    client = worker_client["client"]
    for _ in range(30):
        response = client.post(
            "/api/v1/workers/claim",
            json={
                "worker_id": worker_client["worker_id"],
                "available_slots": slots,
                "task_types": task_types or registry.types(),
            },
        )
        assert response.status_code == 200, response.text
        for task in response.json().get("tasks") or []:
            if task["run_id"] == run_id:
                return task
        if not response.json().get("tasks"):
            return None
    return None


def drain_run(client, account, worker_client, run_id, *, max_rounds=60):
    """Drain a specific run and return its final detail."""
    for _ in range(max_rounds):
        results = drain(worker_client, run_id=run_id, max_rounds=1)
        detail = client.get(f"/api/v1/runs/{run_id}", headers=account["headers"]).json()
        if detail["status"] in {"succeeded", "failed", "cancelled"}:
            return detail
        if not results:
            # Nothing claimable and the run is not terminal: it is waiting on a
            # timer (a retry backoff), so poll rather than spin.
            import time

            time.sleep(0.2)
    return client.get(f"/api/v1/runs/{run_id}", headers=account["headers"]).json()


class _Context:
    """Minimal TaskContext for in-process handler invocation."""

    def __init__(self, task):
        self.task = task
        self.idempotency_key = task["idempotency_key"]
        self.attempt = task["attempt"]
        self.run_id = task["run_id"]
        self.step_key = task["step_key"]
        self.deadline_at = task["deadline_at"]

    def log(self, message, **fields):
        return None

    def cancelled(self):
        return False


def run_definition(name="Exec test", steps=None, **overrides):
    payload = {
        "name": name,
        "description": "execution test",
        "default_max_parallel": 4,
        "steps": steps
        or [
            {"id": "a", "type": "demo.echo", "input": {"value": "A"}, "depends_on": [], "retries": 0, "timeout_seconds": 60},
            {"id": "b", "type": "demo.add", "input": {"values": [1, 2, 3]}, "depends_on": [], "retries": 0, "timeout_seconds": 60},
            {"id": "c", "type": "demo.summarize", "input": {"title": "joined"}, "depends_on": ["a", "b"], "retries": 0, "timeout_seconds": 60},
        ],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_run_completes_and_passes_dependency_outputs_downstream(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(account["headers"], run_definition())
    started = client.post(
        f"/api/v1/workflows/{workflow['id']}/runs",
        json={"input": {"tenant": "acme"}},
        headers=account["headers"],
    )
    assert started.status_code == 201
    run_id = started.json()["id"]

    drain(worker_client)

    detail = client.get(f"/api/v1/runs/{run_id}", headers=account["headers"]).json()
    assert detail["status"] == "succeeded"
    steps = {step["key"]: step for step in detail["steps"]}
    assert all(step["status"] == "succeeded" for step in detail["steps"])

    # Dependency outputs reach the downstream step.
    joined = steps["c"]["output"]
    assert joined["total"] == 6.0  # from demo.add's sum
    assert "a" in joined["upstream_steps"] and "b" in joined["upstream_steps"]
    assert joined["workflow_input_keys"] == ["tenant"]
    assert steps["a"]["output"]["dependencies"] == {}
    assert steps["a"]["duration_seconds"] is not None


def test_worker_receives_resolved_workflow_and_dependency_input_templates(
    client, account, worker_client, workflow_factory
):
    workflow = workflow_factory(
        account["headers"],
        run_definition(
            steps=[
                {
                    "id": "source",
                    "type": "demo.echo",
                    "input": {"value": "source"},
                    "depends_on": [],
                    "timeout_seconds": 60,
                },
                {
                    "id": "consumer",
                    "type": "demo.echo",
                    "input": {
                        "value": "{{steps.source.output.value}}",
                        "label": "tenant-{{input.tenant}}",
                    },
                    "depends_on": ["source"],
                    "timeout_seconds": 60,
                },
            ]
        ),
    )
    started = client.post(
        f"/api/v1/workflows/{workflow['id']}/runs",
        json={"input": {"tenant": "acme"}},
        headers=account["headers"],
    )
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]

    source = claim_for_run(worker_client, run_id)
    assert source is not None
    completed = worker_client["client"].post(
        f"/api/v1/tasks/{source['id']}/complete",
        json={
            "worker_id": worker_client["worker_id"],
            "lease_token": source["lease_token"],
            "output": {"value": "generated value"},
        },
    )
    assert completed.status_code == 200, completed.text

    consumer = claim_for_run(worker_client, run_id)
    assert consumer is not None
    assert consumer["input"] == {
        "value": "generated value",
        "label": "tenant-acme",
    }
    assert consumer["workflow_input"] == {"tenant": "acme"}
    assert consumer["dependency_outputs"] == {"source": {"value": "generated value"}}


def test_missing_runtime_template_path_fails_step_instead_of_breaking_claim(
    client, account, worker_client, workflow_factory
):
    workflow = workflow_factory(
        account["headers"],
        run_definition(
            steps=[
                {
                    "id": "source",
                    "type": "demo.echo",
                    "input": {},
                    "depends_on": [],
                    "timeout_seconds": 60,
                },
                {
                    "id": "consumer",
                    "type": "demo.echo",
                    "input": {"value": "{{steps.source.output.missing}}"},
                    "depends_on": ["source"],
                    "timeout_seconds": 60,
                },
            ]
        ),
    )
    started = client.post(
        f"/api/v1/workflows/{workflow['id']}/runs",
        json={"input": {}},
        headers=account["headers"],
    )
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]

    source = claim_for_run(worker_client, run_id)
    assert source is not None
    completed = worker_client["client"].post(
        f"/api/v1/tasks/{source['id']}/complete",
        json={
            "worker_id": worker_client["worker_id"],
            "lease_token": source["lease_token"],
            "output": {"value": "present"},
        },
    )
    assert completed.status_code == 200, completed.text

    detail = client.get(f"/api/v1/runs/{run_id}", headers=account["headers"]).json()
    steps = {step["key"]: step for step in detail["steps"]}
    assert detail["status"] == "failed"
    assert steps["consumer"]["status"] == "failed"
    assert steps["consumer"]["error"]["code"] == "input_reference_unavailable"
    assert steps["consumer"]["attempts"] == 0


def test_failed_optional_step_does_not_fail_run_and_skips_its_dependents(
    client, account, worker_client, workflow_factory
):
    workflow = workflow_factory(
        account["headers"],
        run_definition(
            steps=[
                {
                    "id": "best_effort",
                    "type": "demo.fail_once",
                    "input": {},
                    "depends_on": [],
                    "retries": 0,
                    "required": False,
                    "timeout_seconds": 60,
                },
                {
                    "id": "uses_best_effort",
                    "type": "demo.echo",
                    "input": {"value": "dependent"},
                    "depends_on": ["best_effort"],
                    "required": False,
                    "timeout_seconds": 60,
                },
                {
                    "id": "independent",
                    "type": "demo.echo",
                    "input": {"value": "independent"},
                    "depends_on": [],
                    "timeout_seconds": 60,
                },
            ]
        ),
    )
    started = client.post(
        f"/api/v1/workflows/{workflow['id']}/runs",
        json={"input": {}},
        headers=account["headers"],
    )
    assert started.status_code == 201, started.text

    detail = drain_run(client, account, worker_client, started.json()["id"])
    steps = {step["key"]: step for step in detail["steps"]}
    assert detail["status"] == "succeeded"
    assert steps["best_effort"]["status"] == "failed"
    assert steps["best_effort"]["required"] is False
    assert steps["uses_best_effort"]["status"] == "skipped"
    assert steps["independent"]["status"] == "succeeded"


def test_required_dependent_of_failed_optional_step_fails_run(
    client, account, worker_client, workflow_factory
):
    workflow = workflow_factory(
        account["headers"],
        run_definition(
            steps=[
                {
                    "id": "best_effort",
                    "type": "demo.fail_once",
                    "input": {},
                    "depends_on": [],
                    "retries": 0,
                    "required": False,
                    "timeout_seconds": 60,
                },
                {
                    "id": "required_dependent",
                    "type": "demo.echo",
                    "input": {"value": "required"},
                    "depends_on": ["best_effort"],
                    "required": True,
                    "timeout_seconds": 60,
                },
            ]
        ),
    )
    started = client.post(
        f"/api/v1/workflows/{workflow['id']}/runs",
        json={"input": {}},
        headers=account["headers"],
    )
    assert started.status_code == 201, started.text

    detail = drain_run(client, account, worker_client, started.json()["id"])
    steps = {step["key"]: step for step in detail["steps"]}
    assert detail["status"] == "failed"
    assert steps["best_effort"]["status"] == "failed"
    assert steps["required_dependent"]["status"] == "skipped"
    assert "1 required step(s) were skipped" in detail["error"]["message"]


def test_run_start_accepts_timezone_aware_available_at(client, account, workflow_factory, monkeypatch):
    from datetime import datetime, timezone

    from app.services import run_service

    workflow = workflow_factory(
        account["headers"],
        run_definition(steps=[{"id": "aware", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 60}]),
    )
    monkeypatch.setattr(run_service, "utcnow", lambda: datetime.now(timezone.utc))

    started = client.post(
        f"/api/v1/workflows/{workflow['id']}/runs",
        json={"input": {}},
        headers=account["headers"],
    )

    assert started.status_code == 201, started.text
    assert started.json()["status"] == "queued"
    cancelled = client.post(f"/api/v1/runs/{started.json()['id']}/cancel", headers=account["headers"])
    assert cancelled.status_code == 200


def test_parallel_steps_are_both_claimable_before_either_finishes(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(
            steps=[
                {"id": "left", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 60},
                {"id": "right", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 60},
                {"id": "after", "type": "demo.echo", "input": {}, "depends_on": ["left"], "timeout_seconds": 60},
            ]
        ),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()

    first_claim = worker_client["client"].post(
        "/api/v1/workers/claim",
        json={"worker_id": worker_client["worker_id"], "available_slots": 4},
    ).json()
    claimed_keys = {task["step_key"] for task in first_claim["tasks"]}
    assert {"left", "right"} <= claimed_keys
    # The dependent step must not be claimable while its dependency is running.
    assert "after" not in claimed_keys


def test_worker_recovers_a_claim_after_its_http_response_was_lost(client, account, worker_client, workflow_factory):
    task_type = "test.claim_response_recovery"
    workflow = workflow_factory(
        account["headers"],
        run_definition(
            steps=[{"id": "recover", "type": task_type, "input": {}, "depends_on": [], "timeout_seconds": 60}]
        ),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"])
    assert run.status_code == 201, run.text
    claim_payload = {
        "worker_id": worker_client["worker_id"],
        "available_slots": 1,
        "task_types": [task_type],
        "in_flight_task_ids": [],
    }

    # The first HTTP response represents a response lost after the claim commit.
    lost_response = worker_client["client"].post("/api/v1/workers/claim", json=claim_payload)
    assert lost_response.status_code == 200, lost_response.text
    first_assignment = lost_response.json()["tasks"][0]
    recovered_response = worker_client["client"].post("/api/v1/workers/claim", json=claim_payload)
    assert recovered_response.status_code == 200, recovered_response.text
    recovered_assignment = recovered_response.json()["tasks"][0]

    assert recovered_assignment["id"] == first_assignment["id"]
    assert recovered_assignment["lease_token"] == first_assignment["lease_token"]
    assert recovered_assignment["attempt"] == first_assignment["attempt"] == 1

    completed = worker_client["client"].post(
        f"/api/v1/tasks/{recovered_assignment['id']}/complete",
        json={
            "worker_id": worker_client["worker_id"],
            "lease_token": recovered_assignment["lease_token"],
            "output": {"recovered": True},
        },
    )
    assert completed.status_code == 200, completed.text


def test_concurrency_budget_is_respected(client, account, worker_client, workflow_factory):
    steps = [{"id": f"s{i}", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 60} for i in range(6)]
    workflow = workflow_factory(account["headers"], run_definition(steps=steps, default_max_parallel=2))
    client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"])
    claimed = worker_client["client"].post(
        "/api/v1/workers/claim",
        json={"worker_id": worker_client["worker_id"], "available_slots": 6},
    ).json()
    assert len(claimed["tasks"]) <= 2


def test_worker_only_receives_task_types_it_supports(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(steps=[{"id": "only", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 60}]),
    )
    client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"])
    claimed = worker_client["client"].post(
        "/api/v1/workers/claim",
        json={"worker_id": worker_client["worker_id"], "available_slots": 4, "task_types": ["demo.add"]},
    ).json()
    assert claimed["tasks"] == []


# --------------------------------------------------------------------------- #
# Retries
# --------------------------------------------------------------------------- #
def test_failed_step_retries_then_succeeds(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(
            steps=[
                {
                    "id": "flaky",
                    "type": "demo.fail_once",
                    "input": {"value": "x"},
                    "depends_on": [],
                    "retries": 2,
                    "backoff_seconds": 0,
                    "backoff_multiplier": 1.0,
                    "timeout_seconds": 60,
                }
            ]
        ),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    detail = drain_run(client, account, worker_client, run["id"])
    step = detail["steps"][0]
    assert step["status"] == "succeeded"
    assert step["attempts"] == 2

    attempts = client.get(
        f"/api/v1/runs/{run['id']}/steps/{step['id']}/attempts", headers=account["headers"]
    ).json()["items"]
    assert [item["attempt"] for item in attempts] == [1, 2]
    assert attempts[0]["status"] == "retrying"
    assert attempts[0]["error"]["type"] == "RuntimeError"
    assert attempts[1]["status"] == "succeeded"


def test_step_fails_permanently_when_retries_are_exhausted(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(
            steps=[
                {"id": "doomed", "type": "demo.fail", "input": {"message": "always"}, "depends_on": [], "retries": 1, "backoff_seconds": 0, "backoff_multiplier": 1.0, "timeout_seconds": 60},
                {"id": "after", "type": "demo.echo", "input": {}, "depends_on": ["doomed"], "timeout_seconds": 60},
            ]
        ),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    detail = drain_run(client, account, worker_client, run["id"])
    steps = {step["key"]: step for step in detail["steps"]}
    assert detail["status"] == "failed"
    assert steps["doomed"]["status"] == "failed"
    assert steps["doomed"]["attempts"] == 2  # initial + 1 retry
    # The stored error is exactly what the worker reported.
    assert steps["doomed"]["error"]["type"] == "RuntimeError"
    assert "always" in steps["doomed"]["error"]["message"]
    assert detail["error"]["code"] == "step_failed"
    # A step whose dependency failed is skipped, not run.
    assert steps["after"]["status"] == "skipped"
    assert steps["after"]["attempts"] == 0
    assert detail["retryable_steps"] == ["doomed"]


def test_retry_delay_grows_exponentially(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(
            steps=[
                {"id": "flaky", "type": "demo.fail_once", "input": {}, "depends_on": [], "retries": 3, "backoff_seconds": 4, "backoff_multiplier": 3.0, "timeout_seconds": 60}
            ]
        ),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    task = claim_for_run(worker_client, run["id"], slots=1)
    assert task is not None
    failed = worker_client["client"].post(
        f"/api/v1/tasks/{task['id']}/fail",
        json={
            "worker_id": worker_client["worker_id"],
            "lease_token": task["lease_token"],
            "error": {"type": "RuntimeError", "message": "boom"},
            "retryable": True,
        },
    ).json()
    # base 4 * 3^0 = 4, with up to 25% jitter.
    assert 3.0 <= failed["retry_in_seconds"] <= 5.0
    assert failed["status"] == "retrying"


def test_non_retryable_failure_fails_immediately(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(
            steps=[{"id": "hard", "type": "demo.echo", "input": {}, "depends_on": [], "retries": 5, "timeout_seconds": 60}]
        ),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    task = claim_for_run(worker_client, run["id"], slots=1)
    assert task is not None
    result = worker_client["client"].post(
        f"/api/v1/tasks/{task['id']}/fail",
        json={
            "worker_id": worker_client["worker_id"],
            "lease_token": task["lease_token"],
            "error": {"type": "UnsupportedTaskType", "message": "cannot run this"},
            "retryable": False,
        },
    ).json()
    assert result["status"] == "failed"
    assert result["retry_in_seconds"] is None
    detail = client.get(f"/api/v1/runs/{run['id']}", headers=account["headers"]).json()
    assert detail["status"] == "failed"


# --------------------------------------------------------------------------- #
# Leases
# --------------------------------------------------------------------------- #
def test_lease_token_is_required_and_never_exposed_to_users(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(steps=[{"id": "one", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 60}]),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    task = claim_for_run(worker_client, run["id"], slots=1)
    assert task is not None
    assert len(task["lease_token"]) > 20

    bad_token = worker_client["client"].post(
        f"/api/v1/tasks/{task['id']}/complete",
        json={"worker_id": worker_client["worker_id"], "lease_token": "not-the-right-token", "output": {}},
    )
    assert bad_token.status_code == 403

    detail = client.get(f"/api/v1/runs/{run['id']}", headers=account["headers"])
    assert task["lease_token"] not in detail.text
    assert "lease_token" not in detail.json()["steps"][0]
    events = client.get(f"/api/v1/runs/{run['id']}/events", headers=account["headers"])
    assert task["lease_token"] not in events.text


def test_another_worker_cannot_complete_a_claimed_task(app, client, account, worker_client, workflow_factory):
    import uuid

    from fastapi.testclient import TestClient

    from app.core.security import generate_token, hash_token
    from app.database import SessionLocal
    from app.models.worker import Worker

    workflow = workflow_factory(
        account["headers"],
        run_definition(steps=[{"id": "one", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 60}]),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    task = claim_for_run(worker_client, run["id"], slots=1)
    assert task is not None

    intruder_id = f"intruder-{uuid.uuid4().hex[:8]}"
    intruder_token = generate_token("wrk")
    with SessionLocal() as db:
        db.add(Worker(id=intruder_id, token_hash=hash_token(intruder_token), max_concurrency=1))
        db.commit()
    with TestClient(app) as intruder:
        response = intruder.post(
            f"/api/v1/tasks/{task['id']}/complete",
            json={"worker_id": intruder_id, "lease_token": task["lease_token"], "output": {}},
            headers={"Authorization": f"Bearer {intruder_token}"},
        )
    assert response.status_code == 409


def test_heartbeat_renews_lease_and_reports_cancellation(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(steps=[{"id": "long", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 300}]),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    task = claim_for_run(worker_client, run["id"], slots=1)
    assert task is not None
    original_deadline = task["deadline_at"]
    original_expiry = task["lease_expires_at"]

    beat = worker_client["client"].post(
        f"/api/v1/tasks/{task['id']}/heartbeat",
        json={"worker_id": worker_client["worker_id"], "lease_token": task["lease_token"], "active_tasks": 1},
    ).json()
    assert beat["cancel_requested"] is False
    assert beat["seconds_remaining"] > 0
    assert beat["deadline_at"] == original_deadline
    assert beat["lease_expires_at"] <= original_deadline
    assert beat["lease_expires_at"] >= original_expiry

    client.post(f"/api/v1/runs/{run['id']}/cancel", headers=account["headers"])
    after_cancel = worker_client["client"].post(
        f"/api/v1/tasks/{task['id']}/heartbeat",
        json={"worker_id": worker_client["worker_id"], "lease_token": task["lease_token"], "active_tasks": 1},
    ).json()
    assert after_cancel["cancel_requested"] is True


def test_heartbeat_rejects_a_different_worker(client, account, worker_client, workflow_factory, db_session):
    import uuid

    from app.core.security import generate_token, hash_token, token_prefix
    from app.models.run import StepRun
    from app.models.worker import Worker

    workflow = workflow_factory(
        account["headers"],
        run_definition(steps=[{"id": "owned", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 60}]),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    task = claim_for_run(worker_client, run["id"], slots=1)
    assert task is not None
    step = db_session.get(StepRun, task["id"])
    assert step is not None
    original_expiry = step.lease_expires_at

    other_id = f"other-worker-{uuid.uuid4().hex[:8]}"
    other_token = generate_token("wrk")
    db_session.add(
        Worker(
            id=other_id,
            name=other_id,
            token_hash=hash_token(other_token),
            token_prefix=token_prefix(other_token),
            max_concurrency=1,
        )
    )
    db_session.commit()

    response = worker_client["client"].post(
        f"/api/v1/tasks/{task['id']}/heartbeat",
        json={"worker_id": other_id, "lease_token": task["lease_token"], "active_tasks": 1},
        headers={"Authorization": f"Bearer {other_token}"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "task_not_assigned"
    db_session.expire_all()
    step = db_session.get(StepRun, task["id"])
    assert step is not None
    assert step.worker_id == worker_client["worker_id"]
    assert step.lease_expires_at == original_expiry


def test_atomic_heartbeat_update_rejects_ownership_lost_after_lease_verification(
    client, account, worker_client, workflow_factory, db_session
):
    from sqlalchemy import update

    from app.core.errors import Conflict
    from app.models.run import StepRun, WorkflowRun
    from app.models.worker import Worker
    from app.services import worker_service

    workflow = workflow_factory(
        account["headers"],
        run_definition(steps=[{"id": "race", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 60}]),
    )
    run_response = client.post(
        f"/api/v1/workflows/{workflow['id']}/runs",
        json={"input": {}},
        headers=account["headers"],
    )
    assert run_response.status_code == 201, run_response.text
    task = claim_for_run(worker_client, run_response.json()["id"], slots=1)
    assert task is not None

    step = db_session.get(StepRun, task["id"])
    run = db_session.get(WorkflowRun, task["run_id"])
    worker = db_session.get(Worker, worker_client["worker_id"])
    assert step is not None and run is not None and worker is not None
    original_expiry = step.lease_expires_at
    db_session.execute(
        update(StepRun)
        .where(StepRun.id == step.id, StepRun.status == "running")
        .values(worker_id="replacement-worker")
    )
    db_session.commit()

    with pytest.raises(Conflict) as error:
        worker_service.heartbeat(db_session, step=step, run=run, worker=worker)

    assert error.value.code == "lease_lost"
    db_session.expire_all()
    current = db_session.get(StepRun, step.id)
    assert current is not None
    assert current.worker_id == "replacement-worker"
    assert current.lease_expires_at == original_expiry


def test_expired_lease_is_reclaimed_and_retried(client, account, worker_client, workflow_factory, db_session):
    """A worker that goes silent loses its task, and the platform retries it."""
    from datetime import datetime, timedelta, timezone

    from app.models.run import StepRun
    from app.services import worker_service

    workflow = workflow_factory(
        account["headers"],
        run_definition(
            steps=[{"id": "abandoned", "type": "demo.echo", "input": {}, "depends_on": [], "retries": 1, "backoff_seconds": 0, "backoff_multiplier": 1.0, "timeout_seconds": 300}]
        ),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    task = claim_for_run(worker_client, run["id"], slots=1)
    assert task is not None

    step = db_session.get(StepRun, task["id"])
    step.lease_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5)
    db_session.commit()

    recovered = worker_service.recover_expired_leases(db_session)
    db_session.commit()
    assert recovered == 1

    db_session.refresh(step)
    assert step.status == "retrying"
    assert step.worker_id is None
    assert step.error["code"] == "lease_expired"

    detail = client.get(f"/api/v1/runs/{run['id']}", headers=account["headers"]).json()
    assert detail["steps"][0]["status"] == "retrying"

    # The task is claimable again by another worker and can finish.
    reclaim = claim_for_run(worker_client, run["id"], slots=1)
    assert reclaim is not None
    assert reclaim["id"] == task["id"]
    assert reclaim["attempt"] == 2


def test_worker_shutdown_releases_in_flight_tasks(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(
            steps=[{"id": "held", "type": "demo.echo", "input": {}, "depends_on": [], "retries": 1, "backoff_seconds": 0, "backoff_multiplier": 1.0, "timeout_seconds": 300}]
        ),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    claimed = claim_for_run(worker_client, run["id"], slots=1)
    assert claimed is not None

    shutdown = worker_client["client"].post(f"/api/v1/workers/{worker_client['worker_id']}/shutdown", json={})
    assert shutdown.status_code == 200
    # claim_for_run may also claim older queued work while searching for this run.
    # Shutdown correctly releases every task held by this worker.
    assert shutdown.json()["released_tasks"] >= 1

    detail = client.get(f"/api/v1/runs/{run['id']}", headers=account["headers"]).json()
    assert detail["steps"][0]["status"] == "retrying"
    assert detail["steps"][0]["worker_id"] is None


# --------------------------------------------------------------------------- #
# Cancellation and retry
# --------------------------------------------------------------------------- #
def test_cancelling_a_queued_run_cancels_pending_steps(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"], run_definition())
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    cancelled = client.post(f"/api/v1/runs/{run['id']}/cancel", headers=account["headers"])
    assert cancelled.status_code == 200
    detail = client.get(f"/api/v1/runs/{run['id']}", headers=account["headers"]).json()
    assert detail["status"] == "cancelled"
    assert all(step["status"] == "cancelled" for step in detail["steps"])


def test_cancelling_a_running_run_marks_it_cancelling(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(steps=[{"id": "long", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 300}]),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    assert claim_for_run(worker_client, run["id"], slots=1) is not None

    response = client.post(f"/api/v1/runs/{run['id']}/cancel", headers=account["headers"]).json()
    assert response["status"] == "cancelling"
    assert response["cancel_requested"] is True

    detail = client.get(f"/api/v1/runs/{run['id']}", headers=account["headers"]).json()
    assert detail["status"] == "cancelling"
    assert detail["steps"][0]["status"] == "running"


def test_cancelling_a_finished_run_is_a_no_op(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(steps=[{"id": "quick", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 60}]),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    drain_run(client, account, worker_client, run["id"])
    response = client.post(f"/api/v1/runs/{run['id']}/cancel", headers=account["headers"]).json()
    assert response["status"] == "succeeded"
    assert response["message"] == "This run has already finished"


def test_retry_failed_steps_resets_and_reruns(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(
            steps=[
                {"id": "boom", "type": "demo.fail", "input": {}, "depends_on": [], "retries": 0, "timeout_seconds": 60},
                {"id": "downstream", "type": "demo.echo", "input": {}, "depends_on": ["boom"], "timeout_seconds": 60},
            ]
        ),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    assert drain_run(client, account, worker_client, run["id"])["status"] == "failed"

    # Make the previously failing step succeed, then retry it.
    client.patch(
        f"/api/v1/workflows/{workflow['id']}",
        json={
            "steps": [
                {"id": "boom", "type": "demo.echo", "input": {"value": "fixed"}, "depends_on": [], "retries": 0, "timeout_seconds": 60},
                {"id": "downstream", "type": "demo.echo", "input": {}, "depends_on": ["boom"], "timeout_seconds": 60},
            ]
        },
        headers=account["headers"],
    )
    client.post(f"/api/v1/workflows/{workflow['id']}/publish", json={"note": "fix"}, headers=account["headers"])

    # A run is pinned to its version, so retrying keeps the original definition:
    # the step fails again, which is the documented and safe behaviour.
    retried = client.post(f"/api/v1/runs/{run['id']}/retry", json={"steps": ["boom"], "reset_downstream": True}, headers=account["headers"])
    assert retried.status_code == 200
    body = retried.json()
    assert body["retried_steps"] == ["boom"]
    assert "downstream" in body["reset_steps"]
    assert body["status"] == "queued"

    final = drain_run(client, account, worker_client, run["id"])
    assert final["status"] == "failed"
    assert {step["key"]: step["status"] for step in final["steps"]} == {"boom": "failed", "downstream": "skipped"}


def test_retry_is_rejected_on_an_active_run(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(steps=[{"id": "slow", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 300}]),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    assert claim_for_run(worker_client, run["id"], slots=1) is not None
    response = client.post(f"/api/v1/runs/{run['id']}/retry", json={}, headers=account["headers"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "run_not_finished"


def test_retry_with_nothing_to_do_is_rejected(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(steps=[{"id": "ok", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 60}]),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    drain_run(client, account, worker_client, run["id"])
    response = client.post(f"/api/v1/runs/{run['id']}/retry", json={}, headers=account["headers"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "nothing_to_retry"


# --------------------------------------------------------------------------- #
# Idempotency, events, history
# --------------------------------------------------------------------------- #
def test_run_idempotency_key_prevents_duplicates(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    payload = {"input": {"x": 1}, "idempotency_key": "nightly-2026-03-01"}
    first = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json=payload, headers=account["headers"]).json()
    second = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json=payload, headers=account["headers"]).json()
    assert first["id"] == second["id"]
    assert second["idempotent_replay"] is True
    listing = client.get(f"/api/v1/workflows/{workflow['id']}/runs", headers=account["headers"]).json()
    assert listing["total"] == 1


def test_run_pins_the_version_it_started_with(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"], publish=False)
    client.post(f"/api/v1/workflows/{workflow['id']}/publish", json={"note": "v1"}, headers=account["headers"])
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    assert run["version"] == 1

    client.patch(f"/api/v1/workflows/{workflow['id']}", json={"description": "v2 draft"}, headers=account["headers"])
    client.post(f"/api/v1/workflows/{workflow['id']}/publish", json={"note": "v2"}, headers=account["headers"])

    detail = client.get(f"/api/v1/runs/{run['id']}", headers=account["headers"]).json()
    assert detail["version"] == 1

    # A run can be pinned explicitly to an older version.
    older = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}, "version": 1}, headers=account["headers"]).json()
    assert older["version"] == 1


def test_starting_a_run_without_a_published_version_is_rejected(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"], publish=False)
    response = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_published_version"


def test_unknown_version_is_rejected(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    response = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}, "version": 99}, headers=account["headers"])
    assert response.status_code == 404


def test_events_describe_the_whole_run_lifecycle(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(
            steps=[
                {"id": "flaky", "type": "demo.fail_once", "input": {}, "depends_on": [], "retries": 1, "backoff_seconds": 0, "backoff_multiplier": 1.0, "timeout_seconds": 60},
                {"id": "after", "type": "demo.echo", "input": {}, "depends_on": ["flaky"], "timeout_seconds": 60},
            ]
        ),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    drain_run(client, account, worker_client, run["id"])

    events = client.get(f"/api/v1/runs/{run['id']}/events", headers=account["headers"]).json()
    types = [event["type"] for event in events["items"]]
    assert "run.created" in types
    assert "step.ready" in types
    assert "step.claimed" in types
    assert "step.retrying" in types
    assert "step.retry_scheduled" in types
    assert "step.succeeded" in types
    assert "run.succeeded" in types
    assert events["latest_seq"] == max(event["seq"] for event in events["items"])

    # Incremental polling: only newer events come back.
    tail = client.get(f"/api/v1/runs/{run['id']}/events", params={"after_seq": events["items"][0]["seq"]}, headers=account["headers"]).json()
    assert all(event["seq"] > events["items"][0]["seq"] for event in tail["items"])


def test_step_logs_are_captured_per_attempt(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(steps=[{"id": "chatty", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 60}]),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    task = claim_for_run(worker_client, run["id"], slots=1)
    assert task is not None
    worker_client["client"].post(
        f"/api/v1/tasks/{task['id']}/complete",
        json={
            "worker_id": worker_client["worker_id"],
            "lease_token": task["lease_token"],
            "output": {"ok": True},
            "logs": [{"level": "info", "message": "step one"}, {"level": "warning", "message": "careful"}],
        },
    )
    detail = client.get(f"/api/v1/runs/{run['id']}", headers=account["headers"]).json()
    step_id = detail["steps"][0]["id"]
    logs = client.get(f"/api/v1/runs/{run['id']}/steps/{step_id}/logs", headers=account["headers"]).json()
    assert [line["message"] for line in logs["lines"]] == ["step one", "careful"]


def test_secret_values_are_redacted_from_logs_and_outputs(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(steps=[{"id": "use_secret", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 60}]),
    )
    client.put(
        f"/api/v1/workflows/{workflow['id']}/secrets/token",
        json={"name": "token", "value": "top-secret-value"},
        headers=account["headers"],
    )
    client.patch(
        f"/api/v1/workflows/{workflow['id']}",
        json={"steps": [{"id": "use_secret", "type": "demo.echo", "input": {"value": {"$secret": "token"}}, "depends_on": [], "timeout_seconds": 60}]},
        headers=account["headers"],
    )
    client.post(f"/api/v1/workflows/{workflow['id']}/publish", json={"note": "with secret"}, headers=account["headers"])
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()

    from app.database import SessionLocal
    from app.models.run import StepRun

    with SessionLocal() as db:
        stored_step = db.query(StepRun).filter(StepRun.run_id == run["id"]).one()
        assert stored_step.input_data == {"value": {"$secret": "token"}}
        assert "top-secret-value" not in str(stored_step.input_data)

    task = claim_for_run(worker_client, run["id"], slots=1)
    assert task is not None
    # The worker receives the plaintext value, because that is what it needs to run.
    assert task["input"]["value"] == "top-secret-value"

    worker_client["client"].post(
        f"/api/v1/tasks/{task['id']}/complete",
        json={
            "worker_id": worker_client["worker_id"],
            "lease_token": task["lease_token"],
            "output": {"api_key": "top-secret-value", "safe": "visible", "note": "credential top-secret-value"},
            "logs": [{"message": "using credential top-secret-value", "token": "top-secret-value"}],
        },
    )
    detail = client.get(f"/api/v1/runs/{run['id']}", headers=account["headers"])
    # Key-based redaction hides credential-shaped fields everywhere.
    assert detail.json()["steps"][0]["output"]["api_key"] == "***redacted***"
    assert detail.json()["steps"][0]["output"]["safe"] == "visible"
    assert "top-secret-value" not in detail.text
    step_id = detail.json()["steps"][0]["id"]
    logs = client.get(f"/api/v1/runs/{run['id']}/steps/{step_id}/logs", headers=account["headers"]).json()
    assert logs["lines"][0]["token"] == "***redacted***"
    assert "top-secret-value" not in str(logs)


def test_dashboard_aggregates_history(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(
        account["headers"],
        run_definition(
            steps=[
                {"id": "ok", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 60},
                {"id": "bad", "type": "demo.fail", "input": {}, "depends_on": [], "retries": 1, "backoff_seconds": 0, "backoff_multiplier": 1.0, "timeout_seconds": 60},
            ]
        ),
    )
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    drain_run(client, account, worker_client, run["id"])

    dashboard = client.get("/api/v1/runs/dashboard", headers=account["headers"]).json()
    stats = dashboard["stats"]
    assert stats["runs_total"] == 1
    assert stats["workflows_total"] == 1
    assert stats["step_attempts"] >= 3  # ok + bad attempt 1 + bad attempt 2
    assert stats["retried_steps"] >= 1
    assert stats["failed_steps"] >= 1
    assert dashboard["recent_runs"][0]["workflow_name"] == "Exec test"
    assert any(item["kind"] == "run" for item in dashboard["recent_activity"])


def test_dashboard_returns_worker_last_seen_metrics(client, account, worker_client):
    from datetime import datetime, timezone

    dashboard = client.get("/api/v1/runs/dashboard", headers=account["headers"])

    assert dashboard.status_code == 200, dashboard.text
    workers = dashboard.json()["workers"]
    current_worker = next(item for item in workers if item["worker_id"] == worker_client["worker_id"])
    assert datetime.fromisoformat(current_worker["last_seen_at"]).tzinfo is not None
    assert current_worker["last_seen_seconds_ago"] < 60


def test_dashboard_normalizes_aware_worker_timestamps():
    from datetime import datetime, timezone

    from app.services.dashboard_service import _as_utc

    value = datetime.now(timezone.utc)

    assert _as_utc(value) == value


def test_worker_stale_check_handles_timezone_aware_timestamp():
    from datetime import datetime, timedelta, timezone

    from app.models.worker import Worker

    worker = Worker(id="timezone-aware-worker", last_seen_at=datetime.now(timezone.utc))

    assert worker.is_stale is False

    worker.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=1)

    assert worker.is_stale is True


def test_run_history_filters(client, account, worker_client, workflow_factory):
    workflow = workflow_factory(account["headers"], run_definition(steps=[{"id": "s", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 60}]))
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    drain_run(client, account, worker_client, run["id"])
    succeeded = client.get("/api/v1/runs", params={"status": "succeeded"}, headers=account["headers"]).json()
    assert succeeded["total"] == 1
    assert client.get("/api/v1/runs", params={"status": "failed"}, headers=account["headers"]).json()["total"] == 0
    by_workflow = client.get("/api/v1/runs", params={"workflow_id": workflow["id"]}, headers=account["headers"]).json()
    assert by_workflow["total"] == 1


def test_deleting_an_active_run_is_rejected(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"]).json()
    response = client.delete(f"/api/v1/runs/{run['id']}", headers=account["headers"])
    assert response.status_code == 409


def test_outbox_records_dispatch_messages(client, account, workflow_factory, db_session):
    """Dispatch is durable: the message commits with the state change."""
    from sqlalchemy import func, select

    from app.models.run import OutboxMessage

    workflow = workflow_factory(
        account["headers"],
        run_definition(steps=[{"id": "s", "type": "demo.echo", "input": {}, "depends_on": [], "timeout_seconds": 60}]),
    )
    client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"])
    count = int(db_session.scalar(select(func.count()).select_from(OutboxMessage).where(OutboxMessage.topic == "task.ready")) or 0)
    assert count >= 1


def test_ops_overview_reports_fleet_and_queue(client, account, worker_client):
    response = client.get("/api/v1/ops/overview", headers=account["headers"])
    assert response.status_code == 200
    body = response.json()
    assert body["dispatch"]["backend"] == "database-polling"
    assert "workers" in body and "scheduler" in body and "steps" in body
    assert body["configuration"]["max_workflow_steps"] >= 1
    # Secrets never appear in the operator view.
    assert "secret_key" not in response.text.lower()
