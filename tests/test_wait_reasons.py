"""Focused coverage for every ``wait_reason`` value in run detail.

Each test builds a real run through the API, nudges it into the state under
test via the database, then asserts the additive ``wait_reason`` reported by
``run_service.wait_reason_for`` (the same function the run-detail endpoint
uses). No worker is started, so nothing moves on its own.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from app.models.run import StepAttempt, StepRun, TaskRateBucket, WorkflowRun
from app.services import run_service


def _definition(name, steps):
    return {"name": name, "description": "wait reason test", "steps": steps}


def _echo(step_id, **extra):
    payload = {"id": step_id, "type": "demo.echo", "input": {}, "queue": "test-wait-reasons"}
    payload.update(extra)
    return payload


def _run_and_steps(client, headers, db_session, workflow_factory, definition):
    workflow = workflow_factory(headers, definition)
    started = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=headers)
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    db_session.expire_all()
    run = db_session.get(WorkflowRun, run_id)
    steps = {step.step_key: step for step in db_session.query(StepRun).filter(StepRun.run_id == run_id).all()}
    return run, steps


def test_waiting_for_dependencies(client, account, db_session, workflow_factory):
    run, steps = _run_and_steps(
        client,
        account["headers"],
        db_session,
        workflow_factory,
        _definition("wait deps", [_echo("a"), _echo("b", depends_on=["a"])]),
    )
    assert run_service.wait_reason_for(db_session, run, steps["b"], steps) == "waiting_for_dependencies"
    # The upstream step itself is only waiting on a worker.
    assert run_service.wait_reason_for(db_session, run, steps["a"], steps) == "waiting_for_worker"


def test_approval_pending(client, account, db_session, workflow_factory):
    run, steps = _run_and_steps(
        client, account["headers"], db_session, workflow_factory, _definition("wait approval", [_echo("a")])
    )
    steps["a"].status = "waiting_approval"
    db_session.commit()
    assert run_service.wait_reason_for(db_session, run, steps["a"], steps) == "approval_pending"


def test_paused_run(client, account, db_session, workflow_factory):
    run, steps = _run_and_steps(
        client, account["headers"], db_session, workflow_factory, _definition("wait paused", [_echo("a")])
    )
    run.status = "paused"
    run.paused_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.commit()
    assert run_service.wait_reason_for(db_session, run, steps["a"], steps) == "paused"


def test_rate_limited(client, account, db_session, workflow_factory, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "task_rate_limits", {"demo.echo": 1})
    run, steps = _run_and_steps(
        client, account["headers"], db_session, workflow_factory, _definition("wait rate", [_echo("a")])
    )
    owner_id = account["user"]["id"]
    bucket_name = "demo.echo"
    key = hashlib.sha256(f"{owner_id}\0{bucket_name}".encode("utf-8")).hexdigest()
    db_session.add(TaskRateBucket(key=key, tokens=0.0, updated_at=datetime.now(timezone.utc)))
    db_session.commit()
    assert run_service.wait_reason_for(db_session, run, steps["a"], steps) == "rate_limited"


def test_concurrency_limited_by_run_budget(client, account, db_session, workflow_factory):
    run, steps = _run_and_steps(
        client,
        account["headers"],
        db_session,
        workflow_factory,
        _definition("wait concurrency", [_echo("a"), _echo("b")]),
    )
    run.max_parallel = 1
    steps["a"].status = "running"
    db_session.commit()
    assert run_service.wait_reason_for(db_session, run, steps["b"], steps) == "concurrency_limited"


def test_circuit_open(client, account, db_session, workflow_factory, monkeypatch):
    from app.config import settings
    from app.models.workflow import Workflow as WorkflowModel

    monkeypatch.setattr(settings, "circuit_breaker_failure_threshold", 1)
    monkeypatch.setattr(settings, "circuit_breaker_open_seconds", 3600)
    run, steps = _run_and_steps(
        client, account["headers"], db_session, workflow_factory, _definition("wait circuit", [_echo("a")])
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    attempt = StepAttempt(
        step_run_id=steps["a"].id,
        attempt_no=1,
        status="failed",
        error={"retryable": True, "message": "boom"},
        started_at=now,
        finished_at=now,
    )
    db_session.add(attempt)
    db_session.commit()
    workflow = db_session.get(WorkflowModel, run.workflow_id)
    assert workflow is not None
    assert run_service.wait_reason_for(db_session, run, steps["a"], steps) == "circuit_open"


def test_waiting_for_worker_when_nothing_blocks(client, account, db_session, workflow_factory):
    run, steps = _run_and_steps(
        client, account["headers"], db_session, workflow_factory, _definition("wait worker", [_echo("a")])
    )
    assert run_service.wait_reason_for(db_session, run, steps["a"], steps) == "waiting_for_worker"


def test_terminal_and_running_steps_have_no_wait_reason(client, account, db_session, workflow_factory):
    run, steps = _run_and_steps(
        client,
        account["headers"],
        db_session,
        workflow_factory,
        _definition("wait none", [_echo("a"), _echo("b")]),
    )
    steps["a"].status = "succeeded"
    steps["b"].status = "running"
    db_session.commit()
    assert run_service.wait_reason_for(db_session, run, steps["a"], steps) is None
    assert run_service.wait_reason_for(db_session, run, steps["b"], steps) is None


def test_wait_reason_surfaces_in_run_detail_payload(client, account, db_session, workflow_factory):
    # End-to-end: the API payload carries the computed reason.
    run, steps = _run_and_steps(
        client,
        account["headers"],
        db_session,
        workflow_factory,
        _definition("wait payload", [_echo("a"), _echo("b", depends_on=["a"])]),
    )
    detail = client.get(f"/api/v1/runs/{run.id}", headers=account["headers"]).json()
    by_key = {step["key"]: step for step in detail["steps"]}
    assert by_key["b"]["wait_reason"] == "waiting_for_dependencies"
    assert by_key["a"]["wait_reason"] == "waiting_for_worker"
