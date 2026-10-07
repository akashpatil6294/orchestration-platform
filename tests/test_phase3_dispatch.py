"""Phase 3 dispatch, fairness, admission-control and dead-letter checks."""
from __future__ import annotations

import time
import urllib.error


def _workflow(workflow_factory, headers, *, name, steps):
    return workflow_factory(
        headers,
        {
            "name": name,
            "description": "Phase 3 dispatch test",
            "default_max_parallel": 8,
            "steps": steps,
        },
    )


def _start(client, headers, workflow_id, **settings):
    response = client.post(
        f"/api/v1/workflows/{workflow_id}/runs",
        json={"input": {}, **settings},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _claim(worker_client, *, slots=1, queues, wait_seconds=0, in_flight=None):
    body = {
        "worker_id": worker_client["worker_id"],
        "available_slots": slots,
        "queues": queues,
        "wait_seconds": wait_seconds,
    }
    if in_flight is not None:
        body["in_flight_task_ids"] = in_flight
    response = worker_client["client"].post("/api/v1/workers/claim", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _complete(worker_client, task):
    response = worker_client["client"].post(
        f"/api/v1/tasks/{task['id']}/complete",
        json={
            "worker_id": worker_client["worker_id"],
            "lease_token": task["lease_token"],
            "output": {"ok": True},
        },
    )
    return response


def test_claim_routes_by_queue_and_step_priority(client, account, worker_client, workflow_factory):
    workflow = _workflow(
        workflow_factory,
        account["headers"],
        name="Phase 3 priority and queues",
        steps=[
            {"id": "normal", "type": "demo.echo", "input": {}, "queue": "phase3-priority", "priority": 2},
            {"id": "urgent", "type": "demo.echo", "input": {}, "queue": "phase3-priority", "priority": 80},
        ],
    )
    run = _start(client, account["headers"], workflow["id"], priority=5)

    wrong_pool = _claim(worker_client, slots=2, queues=["default"])
    assert wrong_pool["tasks"] == []
    gpu_pool = _claim(worker_client, queues=["phase3-priority"])
    assert gpu_pool["task"]["step_key"] == "urgent"
    assert gpu_pool["task"]["priority"] == 80
    assert gpu_pool["task"]["queue"] == "phase3-priority"
    detail = client.get(f"/api/v1/runs/{run['id']}", headers=account["headers"]).json()
    assert detail["priority"] == 5


def test_resource_concurrency_gate_releases_after_completion(client, account, worker_client, workflow_factory):
    workflow = _workflow(
        workflow_factory,
        account["headers"],
        name="Phase 3 resource gate",
        steps=[
            {
                "id": key,
                "type": "demo.echo",
                "input": {},
                "queue": "phase3-resource",
                "concurrency_key": "db-prod",
                "concurrency_limit": 1,
            }
            for key in ("first", "second")
        ],
    )
    _start(client, account["headers"], workflow["id"])
    first_claim = _claim(worker_client, slots=2, queues=["phase3-resource"])
    assert len(first_claim["tasks"]) == 1

    assert _complete(worker_client, first_claim["tasks"][0]).status_code == 200
    second_claim = _claim(worker_client, queues=["phase3-resource"])
    assert second_claim["task"] is not None
    assert second_claim["task"]["id"] != first_claim["tasks"][0]["id"]


def test_queue_concurrency_limit_applies_across_worker_slots(monkeypatch, client, account, worker_client, workflow_factory):
    from app.config import settings

    monkeypatch.setattr(settings, "queue_concurrency_limits", {"phase3-queue-cap": 1})
    workflow = _workflow(
        workflow_factory,
        account["headers"],
        name="Phase 3 queue cap",
        steps=[{"id": key, "type": "demo.echo", "input": {}} for key in ("one", "two")],
    )
    _start(client, account["headers"], workflow["id"], queue="phase3-queue-cap")
    first = _claim(worker_client, slots=2, queues=["phase3-queue-cap"])
    assert len(first["tasks"]) == 1
    assert _complete(worker_client, first["tasks"][0]).status_code == 200
    second = _claim(worker_client, queues=["phase3-queue-cap"])
    assert second["task"] is not None


def test_claims_favor_an_owner_with_fewer_active_tasks(client, account, other_account, worker_client, workflow_factory):
    first_workflow = _workflow(
        workflow_factory,
        account["headers"],
        name="Phase 3 busy owner",
        steps=[
            {"id": "busy", "type": "demo.echo", "input": {}, "queue": "phase3-fair", "priority": 100},
            {"id": "waiting", "type": "demo.echo", "input": {}, "queue": "phase3-fair", "priority": 100},
        ],
    )
    _start(client, account["headers"], first_workflow["id"])
    active = _claim(worker_client, queues=["phase3-fair"])["task"]

    second_workflow = _workflow(
        workflow_factory,
        other_account["headers"],
        name="Phase 3 fresh owner",
        steps=[{"id": "fresh", "type": "demo.echo", "input": {}, "queue": "phase3-fair", "priority": -100}],
    )
    fresh_run = _start(client, other_account["headers"], second_workflow["id"])
    fair_claim = _claim(worker_client, queues=["phase3-fair"])["task"]
    assert fair_claim["run_id"] == fresh_run["id"]
    assert fair_claim["id"] != active["id"]


def test_rate_bucket_limits_claims_per_owner(client, account, worker_client, workflow_factory):
    workflow = _workflow(
        workflow_factory,
        account["headers"],
        name="Phase 3 rate limiter",
        steps=[
            {"id": key, "type": "phase3.rate_limited", "input": {}, "queue": "phase3-rate", "rate_limit_per_minute": 1}
            for key in ("one", "two")
        ],
    )
    _start(client, account["headers"], workflow["id"])
    response = _claim(worker_client, slots=2, queues=["phase3-rate"])
    assert len(response["tasks"]) == 1


def test_failure_status_classification_and_dead_letter_redrive(client, account, other_account, worker_client, workflow_factory):
    workflow = _workflow(
        workflow_factory,
        account["headers"],
        name="Phase 3 DLQ",
        steps=[{"id": "request", "type": "phase3.http", "input": {}, "queue": "phase3-dlq", "retries": 3}],
    )
    run = _start(client, account["headers"], workflow["id"])
    task = _claim(worker_client, queues=["phase3-dlq"])["task"]
    failed = worker_client["client"].post(
        f"/api/v1/tasks/{task['id']}/fail",
        json={
            "worker_id": worker_client["worker_id"],
            "lease_token": task["lease_token"],
            "error": {"type": "HTTPError", "status_code": 400, "message": "bad request"},
        },
    )
    assert failed.status_code == 200, failed.text
    detail = client.get(f"/api/v1/runs/{run['id']}", headers=account["headers"]).json()
    assert detail["steps"][0]["status"] == "failed"
    assert detail["steps"][0]["error"]["retryable"] is False

    listing = client.get("/api/v1/dlq", headers=account["headers"])
    assert listing.status_code == 200
    assert listing.json()["items"][0]["run_id"] == run["id"]
    assert client.get("/api/v1/dlq", headers=other_account["headers"]).json()["items"] == []
    forbidden = client.post(f"/api/v1/dlq/{task['id']}/redrive", headers=other_account["headers"])
    assert forbidden.status_code == 404
    redriven = client.post(f"/api/v1/dlq/{task['id']}/redrive", headers=account["headers"])
    assert redriven.status_code == 200, redriven.text
    assert redriven.json()["retried_steps"] == ["request"]


def test_claim_long_poll_waits_for_dispatch_timeout(client, account, worker_client):
    started = time.monotonic()
    response = _claim(worker_client, queues=["phase3-empty"], wait_seconds=1)
    elapsed = time.monotonic() - started
    assert response["tasks"] == []
    assert elapsed >= 0.9


def test_worker_exception_classification_handles_http_and_programming_errors():
    from app.worker.runtime import classify_exception

    assert classify_exception(urllib.error.HTTPError("https://example.test", 429, "rate limited", {}, None)) is True
    assert classify_exception(urllib.error.HTTPError("https://example.test", 422, "invalid", {}, None)) is False
    assert classify_exception(ValueError("bad configuration")) is False
    assert classify_exception(TimeoutError("upstream timed out")) is True


def test_duplicate_completion_cannot_commit_twice(client, account, worker_client, workflow_factory):
    workflow = _workflow(
        workflow_factory,
        account["headers"],
        name="Phase 3 duplicate completion",
        steps=[{"id": "once", "type": "demo.echo", "input": {}, "queue": "phase3-once"}],
    )
    run = _start(client, account["headers"], workflow["id"])
    task = _claim(worker_client, queues=["phase3-once"])["task"]
    assert _complete(worker_client, task).status_code == 200
    duplicate = _complete(worker_client, task)
    assert duplicate.status_code == 409
    detail = client.get(f"/api/v1/runs/{run['id']}", headers=account["headers"]).json()
    assert detail["steps"][0]["status"] == "succeeded"
    assert detail["steps"][0]["attempts"] == 1


def test_lost_claim_response_is_recovered_from_in_flight_ids(client, account, worker_client, workflow_factory):
    """A claim whose response never reached the worker must be handed back."""
    workflow = _workflow(
        workflow_factory,
        account["headers"],
        name="Phase 3 lost claim response",
        steps=[{"id": key, "type": "demo.echo", "input": {}, "queue": "phase3-lost"} for key in ("one", "two")],
    )
    _start(client, account["headers"], workflow["id"])
    lost = _claim(worker_client, queues=["phase3-lost"])["task"]

    recovered = _claim(worker_client, queues=["phase3-lost"], in_flight=[])["task"]
    assert recovered["id"] == lost["id"]
    assert recovered["lease_token"] == lost["lease_token"]

    other = _claim(worker_client, queues=["phase3-lost"], in_flight=[lost["id"]])["task"]
    assert other["id"] != lost["id"]


def test_redelivered_task_effect_is_applied_exactly_once(client, account, worker_client, workflow_factory, db_session):
    """After a lease loss a redelivered attempt completes once; the stale token is dead."""
    from datetime import datetime, timedelta, timezone

    from app.models.run import StepRun

    workflow = _workflow(
        workflow_factory,
        account["headers"],
        name="Phase 3 exactly once",
        steps=[
            {
                "id": "once", "type": "demo.echo", "input": {"value": "once"}, "queue": "phase3-exactly-once",
                "retries": 2, "backoff_seconds": 0, "retry_jitter": 0,
            }
        ],
    )
    run = _start(client, account["headers"], workflow["id"])
    first = _claim(worker_client, queues=["phase3-exactly-once"])["task"]

    step = db_session.get(StepRun, first["id"])
    step.lease_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5)
    db_session.commit()

    redelivered = _claim(worker_client, queues=["phase3-exactly-once"])["task"]
    assert redelivered["id"] == first["id"]
    assert redelivered["attempt"] == 2
    assert redelivered["idempotency_key"] != first["idempotency_key"]

    stale_complete = _complete(worker_client, first)
    assert stale_complete.status_code == 403, stale_complete.text
    assert _complete(worker_client, redelivered).status_code == 200
    replay = _complete(worker_client, redelivered)
    assert replay.status_code == 409, replay.text

    detail = client.get(f"/api/v1/runs/{run['id']}", headers=account["headers"]).json()
    assert detail["steps"][0]["status"] == "succeeded"
    assert detail["steps"][0]["attempts"] == 2
    events = client.get(f"/api/v1/runs/{run['id']}/events", headers=account["headers"]).json()["items"]
    assert len([event for event in events if event["type"] == "step.succeeded"]) == 1
    assert len([event for event in events if event["type"] == "step.claimed"]) == 2


def test_stale_failure_report_cannot_schedule_a_second_retry(client, account, worker_client, workflow_factory, db_session):
    from datetime import datetime, timedelta, timezone

    from app.models.run import StepRun

    workflow = _workflow(
        workflow_factory,
        account["headers"],
        name="Phase 3 stale failure",
        steps=[
            {
                "id": "work", "type": "demo.echo", "input": {}, "queue": "phase3-stale-fail",
                "retries": 3, "backoff_seconds": 0, "retry_jitter": 0,
            }
        ],
    )
    run = _start(client, account["headers"], workflow["id"])
    first = _claim(worker_client, queues=["phase3-stale-fail"])["task"]

    step = db_session.get(StepRun, first["id"])
    step.lease_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5)
    db_session.commit()
    redelivered = _claim(worker_client, queues=["phase3-stale-fail"])["task"]
    assert redelivered["attempt"] == 2

    stale = worker_client["client"].post(
        f"/api/v1/tasks/{first['id']}/fail",
        json={
            "worker_id": worker_client["worker_id"],
            "lease_token": first["lease_token"],
            "error": {"type": "TimeoutError", "message": "late failure from the old attempt"},
        },
    )
    assert stale.status_code == 403, stale.text

    db_session.expire_all()
    current = db_session.get(StepRun, first["id"])
    assert current.status == "running"
    assert current.attempts == 2
    assert current.error["code"] == "lease_expired"  # the stale failure report changed nothing
    assert _complete(worker_client, redelivered).status_code == 200
    detail = client.get(f"/api/v1/runs/{run['id']}", headers=account["headers"]).json()
    assert detail["steps"][0]["status"] == "succeeded"


def test_repeated_lease_expiry_poisons_the_task(monkeypatch, client, account, worker_client, workflow_factory, db_session):
    from datetime import datetime, timedelta, timezone

    from app.config import settings
    from app.models.run import StepRun

    monkeypatch.setattr(settings, "poison_task_expiry_limit", 2)
    workflow = _workflow(
        workflow_factory,
        account["headers"],
        name="Phase 3 poison task",
        steps=[
            {
                "id": "poison", "type": "demo.echo", "input": {}, "queue": "phase3-poison",
                "retries": 5, "backoff_seconds": 0, "retry_jitter": 0,
            }
        ],
    )
    run = _start(client, account["headers"], workflow["id"])
    task = _claim(worker_client, queues=["phase3-poison"])["task"]

    step = db_session.get(StepRun, task["id"])
    step.lease_expirations = 1  # one earlier lease loss already recorded
    step.lease_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5)
    db_session.commit()

    assert _claim(worker_client, queues=["phase3-poison"])["task"] is None
    detail = client.get(f"/api/v1/runs/{run['id']}", headers=account["headers"]).json()
    assert detail["steps"][0]["status"] == "failed"
    assert detail["steps"][0]["error"]["code"] == "poison_task"
    assert detail["status"] == "failed"
    dead_letters = client.get("/api/v1/dlq", headers=account["headers"]).json()["items"]
    assert dead_letters[0]["step_run_id"] == task["id"]
    assert dead_letters[0]["lease_expirations"] == 2


def test_task_type_circuit_breaker_opens_and_recloses(monkeypatch, client, account, worker_client, workflow_factory, db_session):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    from app.config import settings
    from app.models.run import StepAttempt

    monkeypatch.setattr(settings, "circuit_breaker_failure_threshold", 2)
    monkeypatch.setattr(settings, "circuit_breaker_open_seconds", 30)
    step = {"id": "call", "type": "phase3.cb", "input": {}, "queue": "phase3-cb-a", "retries": 0}
    workflow = _workflow(workflow_factory, account["headers"], name="Phase 3 circuit breaker", steps=[step])

    failed_steps = []
    for _ in range(2):
        _start(client, account["headers"], workflow["id"])
        task = _claim(worker_client, queues=["phase3-cb-a"])["task"]
        failed = worker_client["client"].post(
            f"/api/v1/tasks/{task['id']}/fail",
            json={
                "worker_id": worker_client["worker_id"],
                "lease_token": task["lease_token"],
                "error": {"type": "TimeoutError", "message": "upstream timed out"},
            },
        )
        assert failed.status_code == 200, failed.text
        failed_steps.append(task["id"])

    other_queue = {"id": "call", "type": "phase3.cb", "input": {}, "queue": "phase3-cb-b", "retries": 0}
    blocked = _workflow(workflow_factory, account["headers"], name="Phase 3 circuit breaker blocked", steps=[other_queue])
    _start(client, account["headers"], blocked["id"])
    assert _claim(worker_client, queues=["phase3-cb-b"])["task"] is None

    # Backdate the recorded failures instead of sleeping: the breaker window closes.
    backdated = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=120)
    db_session.execute(
        update(StepAttempt)
        .where(StepAttempt.step_run_id.in_(failed_steps))
        .values(finished_at=backdated, started_at=backdated)
    )
    db_session.commit()

    revived = _claim(worker_client, queues=["phase3-cb-b"])["task"]
    assert revived is not None


def test_global_concurrency_cap_limits_running_steps_across_queues(monkeypatch, client, account, worker_client, workflow_factory, db_session):
    from sqlalchemy import func, select

    from app.config import settings
    from app.models.run import StepRun

    # The global cap counts every running step in the database, so allow exactly
    # one more than is already running rather than assuming an empty system.
    running_now = int(db_session.scalar(select(func.count()).select_from(StepRun).where(StepRun.status == "running")) or 0)
    monkeypatch.setattr(settings, "max_global_running_tasks", running_now + 1)
    workflow = _workflow(
        workflow_factory,
        account["headers"],
        name="Phase 3 global cap",
        steps=[
            {"id": "first", "type": "demo.echo", "input": {}, "queue": "phase3-global-a"},
            {"id": "second", "type": "demo.echo", "input": {}, "queue": "phase3-global-b"},
        ],
    )
    _start(client, account["headers"], workflow["id"])

    first = _claim(worker_client, slots=2, queues=["phase3-global-a"])["tasks"]
    assert len(first) == 1
    assert _claim(worker_client, queues=["phase3-global-b"])["task"] is None

    assert _complete(worker_client, first[0]).status_code == 200
    released = _claim(worker_client, queues=["phase3-global-b"])["task"]
    assert released is not None
    assert released["step_key"] == "second"


def test_task_type_rate_limit_from_settings_caps_claims(monkeypatch, client, account, worker_client, workflow_factory):
    from app.config import settings

    monkeypatch.setattr(settings, "task_rate_limits", {"phase3.cfgrate": 1})
    workflow = _workflow(
        workflow_factory,
        account["headers"],
        name="Phase 3 configured rate limit",
        steps=[
            {"id": key, "type": "phase3.cfgrate", "input": {}, "queue": "phase3-cfgrate"}
            for key in ("one", "two")
        ],
    )
    _start(client, account["headers"], workflow["id"])
    assert len(_claim(worker_client, slots=2, queues=["phase3-cfgrate"])["tasks"]) == 1


def test_clock_skew_uses_server_timestamps(client, account, worker_client, workflow_factory, db_session):
    """Work scheduled in the future waits, and a late result cannot beat the deadline."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.models.run import StepRun

    workflow = _workflow(
        workflow_factory,
        account["headers"],
        name="Phase 3 clock skew",
        steps=[{"id": "skew", "type": "demo.echo", "input": {}, "queue": "phase3-skew", "timeout_seconds": 60}],
    )
    run = _start(client, account["headers"], workflow["id"])
    step = db_session.scalar(select(StepRun).where(StepRun.run_id == run["id"]))
    future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=60)
    step.available_at = future
    db_session.commit()

    empty = _claim(worker_client, queues=["phase3-skew"])
    assert empty["task"] is None
    assert empty["server_time"] is not None

    step.available_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    db_session.commit()
    task = _claim(worker_client, queues=["phase3-skew"])["task"]
    assert task is not None

    step.deadline_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    db_session.commit()
    late = _complete(worker_client, task)
    assert late.status_code == 409, late.text
    assert late.json()["error"]["code"] == "deadline_exceeded"


def test_engine_dispose_between_claims_keeps_the_protocol_working(client, account, worker_client, workflow_factory):
    """Disposing the pool (as after a database restart) must not break claims."""
    from app.database import engine

    workflow = _workflow(
        workflow_factory,
        account["headers"],
        name="Phase 3 reconnect",
        steps=[{"id": key, "type": "demo.echo", "input": {}, "queue": "phase3-reconnect"} for key in ("one", "two")],
    )
    run = _start(client, account["headers"], workflow["id"])
    first = _claim(worker_client, queues=["phase3-reconnect"])["task"]
    engine.dispose()
    second = _claim(worker_client, queues=["phase3-reconnect"], in_flight=[first["id"]])["task"]
    assert second["id"] != first["id"]

    assert _complete(worker_client, first).status_code == 200
    assert _complete(worker_client, second).status_code == 200
    detail = client.get(f"/api/v1/runs/{run['id']}", headers=account["headers"]).json()
    assert detail["status"] == "succeeded"


def test_dead_letter_redrive_is_idempotent_and_emits_one_event(client, account, worker_client, workflow_factory):
    workflow = _workflow(
        workflow_factory,
        account["headers"],
        name="Phase 3 redrive idempotency",
        steps=[{"id": "request", "type": "phase3.http", "input": {}, "queue": "phase3-redrive-once", "retries": 0}],
    )
    run = _start(client, account["headers"], workflow["id"])
    task = _claim(worker_client, queues=["phase3-redrive-once"])["task"]
    failed = worker_client["client"].post(
        f"/api/v1/tasks/{task['id']}/fail",
        json={
            "worker_id": worker_client["worker_id"],
            "lease_token": task["lease_token"],
            "error": {"type": "HTTPError", "status_code": 400, "message": "bad request"},
        },
    )
    assert failed.status_code == 200, failed.text

    first = client.post(f"/api/v1/dlq/{task['id']}/redrive", headers=account["headers"])
    assert first.status_code == 200, first.text
    assert first.json()["retried_steps"] == ["request"]
    assert first.json()["already_redriven"] is False

    second = client.post(f"/api/v1/dlq/{task['id']}/redrive", headers=account["headers"])
    assert second.status_code == 200, second.text
    assert second.json()["already_redriven"] is True
    assert second.json()["retried_steps"] == []

    events = client.get(f"/api/v1/runs/{run['id']}/events", headers=account["headers"]).json()["items"]
    assert len([event for event in events if event["type"] == "run.retry_requested"]) == 1


def test_worker_can_reregister_after_a_graceful_drain(client, account, worker_client, workflow_factory):
    """Draining a worker releases its task and the worker can come back online."""
    workflow = _workflow(
        workflow_factory,
        account["headers"],
        name="Phase 3 drain and restart",
        steps=[
            {
                "id": "drain", "type": "demo.echo", "input": {}, "queue": "phase3-drain",
                "retries": 2, "backoff_seconds": 0, "retry_jitter": 0,
            }
        ],
    )
    _start(client, account["headers"], workflow["id"])
    task = _claim(worker_client, queues=["phase3-drain"])["task"]

    shutdown = worker_client["client"].post(f"/api/v1/workers/{worker_client['worker_id']}/shutdown", json={})
    assert shutdown.status_code == 200, shutdown.text
    assert shutdown.json()["released_tasks"] == 1
    assert shutdown.json()["active"] is False

    blocked = worker_client["client"].post(
        "/api/v1/workers/claim",
        json={"worker_id": worker_client["worker_id"], "available_slots": 1, "queues": ["phase3-drain"], "wait_seconds": 0},
    )
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["error"]["code"] == "worker_inactive"

    registered = worker_client["client"].post(
        "/api/v1/workers/register",
        json={
            "worker_id": worker_client["worker_id"],
            "name": "restarted",
            "task_types": ["demo.echo"],
            "queues": ["phase3-drain"],
            "max_concurrency": 1,
        },
    )
    assert registered.status_code == 201, registered.text
    assert registered.json()["active"] is True

    reclaimed = _claim(worker_client, queues=["phase3-drain"])["task"]
    assert reclaimed is not None
    assert reclaimed["id"] == task["id"]
    assert reclaimed["attempt"] == 2

