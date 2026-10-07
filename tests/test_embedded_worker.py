"""Stage B: the embedded worker executes runs with no external worker process."""
from __future__ import annotations

import time

import pytest


TEST_QUEUE = "test-embedded"


def _start_run(client, account_headers, workflow_factory, steps, name="embedded e2e", queue=TEST_QUEUE):
    workflow = workflow_factory(
        account_headers,
        {
            "name": name,
            "description": "embedded worker test",
            "steps": [
                {"id": step_id, "type": task_type, "input": task_input, "queue": queue}
                for step_id, task_type, task_input in steps
            ],
        },
    )
    response = client.post(f"/api/v1/workflows/{workflow['id']}/runs", headers=account_headers, json={"input": {}})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _wait_for_status(client, account_headers, run_id, statuses, timeout=45.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}", headers=account_headers)
        assert response.status_code == 200, response.text
        last = response.json()["status"]
        if last in statuses:
            return last
        time.sleep(0.5)
    raise AssertionError(f"run {run_id} did not reach {statuses} in {timeout}s (last={last})")


@pytest.fixture()
def embedded_worker(monkeypatch):
    """A started in-process worker bound to the test app; stopped on teardown."""
    from app.config import settings
    from app.main import app
    from app.worker.embedded import EmbeddedWorker

    monkeypatch.setattr(settings, "embedded_worker_enabled", True)
    monkeypatch.setattr(settings, "embedded_worker_concurrency", 2)
    # No long-polling in tests: claims return immediately so the worker thread
    # stops promptly on teardown and never leaks a stale active row.
    monkeypatch.setattr(settings, "worker_long_poll_seconds", 0)
    # A dedicated queue: the shared test database holds orphaned queued steps
    # from other test files, and the embedded worker must not pick those up.
    monkeypatch.setattr(settings, "embedded_worker_queues", TEST_QUEUE)
    handle = EmbeddedWorker(app)
    handle.start()
    try:
        yield handle
    finally:
        handle.stop()


def test_embedded_worker_runs_workflow_to_succeeded(client, account, workflow_factory, embedded_worker):
    run_id = _start_run(
        client,
        account["headers"],
        workflow_factory,
        [("one", "demo.echo", {"text": "hello"}), ("two", "demo.add", {"numbers": [1, 2, 3]})],
    )
    assert _wait_for_status(client, account["headers"], run_id, {"succeeded"}) == "succeeded"
    detail = client.get(f"/api/v1/runs/{run_id}", headers=account["headers"]).json()
    assert all(step["status"] == "succeeded" for step in detail["steps"])
    assert all(step["attempts"] == 1 for step in detail["steps"])
    # The embedded worker (and no other worker) executed both steps.
    assert embedded_worker._worker.processed == 2


def test_disabled_embedded_worker_leaves_run_queued_with_wait_reason(client, account, workflow_factory, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "embedded_worker_enabled", False)
    run_id = _start_run(
        client,
        account["headers"],
        workflow_factory,
        [("one", "demo.echo", {"text": "hello"})],
        name="embedded disabled",
        queue="test-embedded-idle",
    )
    time.sleep(2.0)  # no worker exists; nothing should move
    detail = client.get(f"/api/v1/runs/{run_id}", headers=account["headers"]).json()
    assert detail["status"] == "queued"
    assert detail["steps"][0]["wait_reason"] == "waiting_for_worker"

    capacity = client.get("/api/v1/ops/capacity", headers=account["headers"]).json()
    assert capacity["queues"]["test-embedded-idle"]["queued_steps"] >= 1
    # No active worker covers this queue/task-type pair (other tests' workers
    # may exist, but none claim this queue).
    assert not any(
        "demo.echo" in (worker["task_types"] or []) and "test-embedded-idle" in (worker["queues"] or [])
        for worker in capacity["workers"]
    )


def test_capacity_does_not_leak_other_owners_queue_contents(client, account, workflow_factory):
    # A second account with its own queued run in its own queue.
    import uuid

    email = f"other-{uuid.uuid4().hex[:8]}@example.com"
    response = client.post("/api/v1/auth/register", json={"email": email, "password": "testpassword123"})
    assert response.status_code == 201, response.text
    other_headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    _start_run(
        client,
        other_headers,
        workflow_factory,
        [("one", "demo.echo", {"text": "secret"})],
        name="other tenant run",
        queue="other-tenant-queue",
    )

    capacity = client.get("/api/v1/ops/capacity", headers=account["headers"]).json()
    # Aggregate worker info is fine; queued contents are not exposed.
    assert "workers" in capacity
    assert capacity["queued_owner_counts"].get("someone-else") is None
    assert capacity["queued_owner_counts"].get(account["user"]["id"], 0) == 0
    # The other tenant's queue and demand are invisible to a non-admin.
    assert "other-tenant-queue" not in capacity["queues"]
    assert all(entry["queued_steps"] == 0 for entry in capacity["queues"].values())
    assert "secret" not in str(capacity)


def test_two_embedded_workers_never_double_claim(client, account, workflow_factory, monkeypatch):
    from app.config import settings
    from app.main import app
    from app.worker.embedded import EmbeddedWorker

    monkeypatch.setattr(settings, "embedded_worker_enabled", True)
    monkeypatch.setattr(settings, "embedded_worker_concurrency", 4)
    monkeypatch.setattr(settings, "worker_long_poll_seconds", 0)
    monkeypatch.setattr(settings, "embedded_worker_queues", TEST_QUEUE)
    first = EmbeddedWorker(app)
    second = EmbeddedWorker(app)
    assert first.worker_id != second.worker_id
    first.start()
    second.start()
    try:
        run_id = _start_run(
            client,
            account["headers"],
            workflow_factory,
            [(f"step-{i}", "demo.echo", {"i": i}) for i in range(6)],
            name="embedded double claim",
        )
        assert _wait_for_status(client, account["headers"], run_id, {"succeeded"}, timeout=60.0) == "succeeded"
    finally:
        first.stop()
        second.stop()
    detail = client.get(f"/api/v1/runs/{run_id}", headers=account["headers"]).json()
    for step in detail["steps"]:
        assert step["attempts"] == 1, f"{step['key']} executed {step['attempts']} times"


def test_embedded_worker_drain_releases_lease_on_stop(client, account, workflow_factory, embedded_worker):
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models.run import StepRun

    run_id = _start_run(
        client,
        account["headers"],
        workflow_factory,
        [("slow", "demo.sleep", {"seconds": 300})],
        name="embedded drain",
    )
    # Wait until the worker holds the lease.
    deadline = time.monotonic() + 30.0
    claimed = False
    while time.monotonic() < deadline:
        with SessionLocal() as db:
            step = db.scalar(select(StepRun).where(StepRun.run_id == run_id))
            if step is not None and step.status == "running" and step.worker_id == embedded_worker.worker_id:
                claimed = True
                break
        time.sleep(0.5)
    assert claimed, "embedded worker never claimed the sleep step"

    embedded_worker.stop()  # drain: graceful shutdown releases the lease
    # The sleep outlasts the shutdown grace period, so the worker must hand the
    # lease back instead of waiting for the task to finish.
    deadline = time.monotonic() + 60.0
    released = False
    while time.monotonic() < deadline:
        with SessionLocal() as db:
            step = db.scalar(select(StepRun).where(StepRun.run_id == run_id))
            if step.worker_id != embedded_worker.worker_id or step.status != "running":
                released = True
                break
        time.sleep(0.5)
    assert released, "lease not released on drain"


def _make_admin(db_session, account):
    from sqlalchemy import text

    db_session.execute(text("UPDATE users SET is_admin = 1 WHERE id = :id"), {"id": account["user"]["id"]})
    db_session.commit()


def test_admin_can_mint_worker_token_shown_once(client, account, db_session):
    _make_admin(db_session, account)
    response = client.post(
        "/api/v1/workers",
        json={"worker_id": "minted-1", "queues": ["default"], "task_types": ["demo.echo"]},
        headers=account["headers"],
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    token = payload["token"]
    assert token.startswith("wrk_")
    assert payload["token_prefix"]
    # Plaintext is never stored: the row holds only hash/prefix.
    from app.models.worker import Worker as WorkerRow

    row = db_session.get(WorkerRow, "minted-1")
    assert row is not None
    assert row.token_hash != token
    assert token not in (row.token_hash or "")
    # The minted credential authenticates as a worker.
    claimed = client.post(
        "/api/v1/workers/claim",
        json={"worker_id": "minted-1", "available_slots": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert claimed.status_code == 200, claimed.text


def test_non_admin_cannot_mint_worker_token(client, account):
    response = client.post("/api/v1/workers", json={"worker_id": "minted-2"}, headers=account["headers"])
    assert response.status_code == 403


def test_on_demand_worker_lifecycle(client, account, db_session, monkeypatch):
    from app.config import settings

    _make_admin(db_session, account)
    monkeypatch.setattr(settings, "embedded_worker_enabled", True)
    monkeypatch.setattr(settings, "worker_long_poll_seconds", 0)

    status = client.get("/api/v1/ops/embedded-worker", headers=account["headers"]).json()
    assert status["running"] is False

    first = client.post("/api/v1/ops/embedded-worker/start", headers=account["headers"]).json()
    assert first["status"] == "started"
    # Idempotent: a second start returns the same running worker.
    second = client.post("/api/v1/ops/embedded-worker/start", headers=account["headers"]).json()
    assert second["worker_id"] == first["worker_id"]

    status = client.get("/api/v1/ops/embedded-worker", headers=account["headers"]).json()
    assert status["running"] is True
    assert status["worker_id"] == first["worker_id"]

    stopped = client.post("/api/v1/ops/embedded-worker/stop", headers=account["headers"]).json()
    assert stopped["status"] == "stopped"
    status = client.get("/api/v1/ops/embedded-worker", headers=account["headers"]).json()
    assert status["running"] is False

    # Stopping again is a no-op, not an error.
    stopped = client.post("/api/v1/ops/embedded-worker/stop", headers=account["headers"]).json()
    assert stopped["status"] == "not_running"


def test_on_demand_start_requires_admin(client, account, db_session):
    response = client.post("/api/v1/ops/embedded-worker/start", headers=account["headers"])
    assert response.status_code == 403
    response = client.post("/api/v1/ops/embedded-worker/stop", headers=account["headers"])
    assert response.status_code == 403


def test_on_demand_start_refused_when_disabled(client, account, db_session, monkeypatch):
    from app.config import settings

    _make_admin(db_session, account)
    monkeypatch.setattr(settings, "embedded_worker_enabled", False)
    response = client.post("/api/v1/ops/embedded-worker/start", headers=account["headers"])
    assert response.status_code == 409
