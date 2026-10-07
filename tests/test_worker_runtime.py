from __future__ import annotations

import threading
import time


def test_heartbeat_interval_must_be_shorter_than_the_lease():
    import pytest

    from app.config import Settings

    with pytest.raises(ValueError, match="WORKER_HEARTBEAT_INTERVAL must be less than LEASE_SECONDS"):
        Settings(environment="test", lease_seconds=30, worker_heartbeat_interval=30)


def test_api_client_converts_socket_timeouts_to_retryable_api_errors(monkeypatch):
    import pytest

    from app.worker.runtime import ApiClient, ApiError

    def timeout(*_args, **_kwargs):
        raise TimeoutError("socket timed out")

    monkeypatch.setattr("urllib.request.urlopen", timeout)
    api = ApiClient("http://orchestrator.test", "worker-token", timeout=0.01)

    for request in (lambda: api.post("/claim", {}), lambda: api.get("/health")):
        with pytest.raises(ApiError, match="timed out") as error:
            request()
        assert error.value.status == 0
        assert error.value.message == "Request to the orchestrator API timed out"


def test_api_client_converts_connection_resets_to_retryable_api_errors(monkeypatch):
    import pytest

    from app.worker.runtime import ApiClient, ApiError

    def connection_reset(*_args, **_kwargs):
        raise ConnectionResetError("remote host reset the connection")

    monkeypatch.setattr("urllib.request.urlopen", connection_reset)
    api = ApiClient("http://orchestrator.test", "worker-token", timeout=0.01)

    for request in (lambda: api.post("/claim", {}), lambda: api.get("/health")):
        with pytest.raises(ApiError, match="interrupted") as error:
            request()
        assert error.value.status == 0
        assert error.value.message == "Connection to the orchestrator API was interrupted"


def test_worker_claim_reports_task_ids_it_already_received():
    from app.worker.runtime import RunningTask, Worker

    class RecordingApi:
        timeout = 30.0

        def __init__(self):
            self.claim_bodies = []

        def post(self, path, body=None, *, timeout=None):
            if path.endswith("/claim"):
                self.claim_bodies.append(body)
            return {"tasks": []}

    worker = Worker(token="worker-secret", worker_id="claim-recovery-test")
    worker.api = RecordingApi()
    try:
        assert worker._claim_once() == 0
        assert worker.api.claim_bodies[-1]["in_flight_task_ids"] == []

        state = RunningTask(
            task_id="already-received",
            step_key="step",
            run_id="run",
            attempt=1,
            lease_token="lease-secret",
            task={},
            idempotency_key="run:step:1",
        )
        with worker._running_lock:
            worker._running[state.task_id] = state

        assert worker._claim_once() == 0
        assert worker.api.claim_bodies[-1]["in_flight_task_ids"] == ["already-received"]
    finally:
        worker._pool.shutdown(wait=True)


def test_worker_runtime_registers_and_executes_demo_echo(client, account, worker_client, workflow_factory):
    import time

    from app.worker.handlers import echo  # noqa: F401
    from app.worker.registry import registry
    from app.worker.runtime import Worker

    class TestApi:
        timeout = 30.0

        def post(self, path, body=None, *, timeout=None):
            response = worker_client["client"].post(path, json=body)
            assert response.status_code < 400, response.text
            return response.json()

    workflow = workflow_factory(
        account["headers"],
        {
            "name": "Worker runtime test",
            "description": "Exercise the worker runtime against the API",
            "default_max_parallel": 1,
            "steps": [
                {
                    "id": "echo",
                    "type": "demo.echo",
                    "input": {"value": "worker-runtime"},
                    "depends_on": [],
                    "retries": 0,
                    "timeout_seconds": 60,
                }
            ],
        },
    )
    started = client.post(
        f"/api/v1/workflows/{workflow['id']}/runs",
        json={"input": {"tenant": "runtime-test"}},
        headers=account["headers"],
    )
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]

    worker = Worker(
        token=worker_client["token"],
        worker_id=worker_client["worker_id"],
        registry=registry,
        poll_seconds=0.01,
    )
    worker.api = TestApi()
    try:
        assert worker.register()["worker_id"] == worker_client["worker_id"]
        deadline = time.monotonic() + 5
        detail = None
        while time.monotonic() < deadline:
            worker._claim_once()
            while worker._inflight_count() and time.monotonic() < deadline:
                time.sleep(0.01)
            detail_response = client.get(f"/api/v1/runs/{run_id}", headers=account["headers"])
            assert detail_response.status_code == 200, detail_response.text
            detail = detail_response.json()
            if detail["status"] in {"succeeded", "failed", "cancelled"}:
                break

        assert detail is not None
        assert detail["status"] == "succeeded"
        assert detail["steps"][0]["status"] == "succeeded"
        assert detail["steps"][0]["output"]["value"] == "worker-runtime"
    finally:
        worker.shutdown()


def test_worker_runtime_supplies_handler_task_context_without_lease_token():
    from app.worker.handlers import echo
    from app.worker.registry import Handler, TaskRegistry
    from app.worker.runtime import Worker

    class RecordingApi:
        def __init__(self):
            self.calls = []
            self.called = threading.Event()
            self.timeout = 30.0

        def post(self, path, body=None, *, timeout=None):
            self.calls.append((path, body))
            self.called.set()
            return {}

    captured_context = {}

    def echo_with_context(payload, context):
        captured_context["task"] = context.task
        captured_context["idempotency_key"] = context.idempotency_key
        captured_context["deadline_at"] = context.deadline_at
        return echo(payload, context)

    registry = TaskRegistry()
    registry.add(Handler(task_type="demo.echo", func=echo_with_context))
    task = {
        "id": "task-1",
        "run_id": "run-1",
        "step_key": "echo",
        "attempt": 1,
        "lease_token": "lease-secret",
        "idempotency_key": "run-1:echo:1",
        "deadline_at": None,
        "type": "demo.echo",
        "input": {"value": "hello"},
        "workflow_input": {"tenant": "acme"},
        "dependency_outputs": {"upstream": {"value": "ready"}},
    }
    worker = Worker(token="worker-secret", worker_id="runtime-test", registry=registry)
    worker.api = RecordingApi()

    try:
        worker._submit(task)
        assert worker.api.called.wait(timeout=5)

        assert len(worker.api.calls) == 1
        path, body = worker.api.calls[0]
        assert path == "/api/v1/tasks/task-1/complete"
        assert body["output"]["value"] == "hello"
        assert body["output"]["dependencies"] == {"upstream": {"value": "ready"}}
        assert captured_context["task"]["dependency_outputs"] == {"upstream": {"value": "ready"}}
        assert "lease_token" not in captured_context["task"]
        assert captured_context["idempotency_key"] == task["idempotency_key"]
    finally:
        worker._pool.shutdown(wait=True)


def _runtime_task(task_type: str, *, heartbeat_interval_seconds: int = 1) -> dict:
    return {
        "id": "task-heartbeat-test",
        "run_id": "run-heartbeat-test",
        "step_key": "long",
        "attempt": 1,
        "lease_token": "lease-secret",
        "idempotency_key": "run-heartbeat-test:long:1",
        "deadline_at": "2099-01-01T00:00:00Z",
        "heartbeat_interval_seconds": heartbeat_interval_seconds,
        "type": task_type,
        "input": {},
        "workflow_input": {},
        "dependency_outputs": {},
    }


def test_short_task_starts_and_cleans_up_per_task_heartbeat():
    from app.worker.registry import Handler, TaskRegistry
    from app.worker.runtime import Worker

    started = threading.Event()
    release = threading.Event()
    calls = []

    def handler(_payload, _context):
        started.set()
        assert release.wait(timeout=5)
        return {"ok": True}

    class Api:
        timeout = 30.0

        def post(self, path, body=None, *, timeout=None):
            calls.append((path, body))
            return {"lease_expires_at": "2099-01-01T00:00:30Z", "cancel_requested": False}

    registry = TaskRegistry()
    registry.add(Handler(task_type="test.short", func=handler))
    worker = Worker(token="worker-secret", worker_id="heartbeat-short", registry=registry)
    worker.api = Api()
    task = _runtime_task("test.short", heartbeat_interval_seconds=1)
    try:
        worker._submit(task)
        assert started.wait(timeout=5)
        state = worker._running[task["id"]]
        release.set()
        deadline = time.monotonic() + 5
        while worker._inflight_count() and time.monotonic() < deadline:
            time.sleep(0.01)

        assert not worker._inflight_count()
        assert state.heartbeat_stop.is_set()
        assert state.heartbeat_thread is not None and not state.heartbeat_thread.is_alive()
        assert any(path.endswith("/complete") for path, _ in calls)
        assert not any(path.endswith("/heartbeat") for path, _ in calls)
    finally:
        release.set()
        worker._pool.shutdown(wait=True)


def test_failed_task_stops_heartbeat_and_preserves_retryable_failure():
    from app.worker.registry import Handler, TaskRegistry
    from app.worker.runtime import Worker

    started = threading.Event()
    release = threading.Event()
    calls = []

    def handler(_payload, _context):
        started.set()
        assert release.wait(timeout=5)
        raise RuntimeError("temporary provider failure")

    class Api:
        timeout = 30.0

        def post(self, path, body=None, *, timeout=None):
            calls.append((path, body))
            return {"lease_expires_at": "2099-01-01T00:00:30Z", "cancel_requested": False}

    registry = TaskRegistry()
    registry.add(Handler(task_type="test.failure", func=handler))
    worker = Worker(token="worker-secret", worker_id="heartbeat-failure", registry=registry)
    worker.api = Api()
    task = _runtime_task("test.failure")
    try:
        worker._submit(task)
        assert started.wait(timeout=5)
        state = worker._running[task["id"]]
        release.set()
        deadline = time.monotonic() + 5
        while worker._inflight_count() and time.monotonic() < deadline:
            time.sleep(0.01)

        failures = [body for path, body in calls if path.endswith("/fail")]
        assert len(failures) == 1
        assert failures[0]["retryable"] is True
        assert failures[0]["error"]["message"] == "temporary provider failure"
        assert state.heartbeat_thread is not None and not state.heartbeat_thread.is_alive()
    finally:
        release.set()
        worker._pool.shutdown(wait=True)


def test_heartbeat_stops_and_discards_result_after_ownership_loss():
    from app.worker.registry import Handler, TaskRegistry
    from app.worker.runtime import ApiError, Worker

    started = threading.Event()
    calls = []

    def handler(_payload, context):
        started.set()
        deadline = time.monotonic() + 5
        while not context.cancelled() and time.monotonic() < deadline:
            time.sleep(0.01)
        return {"must_not_be_reported": True}

    class Api:
        timeout = 30.0

        def post(self, path, body=None, *, timeout=None):
            calls.append((path, body))
            if path.endswith("/heartbeat"):
                raise ApiError(409, "The task lease has expired", code="lease_expired")
            return {}

    registry = TaskRegistry()
    registry.add(Handler(task_type="test.ownership", func=handler))
    worker = Worker(token="worker-secret", worker_id="heartbeat-ownership", registry=registry)
    worker.api = Api()
    task = _runtime_task("test.ownership", heartbeat_interval_seconds=1)
    try:
        task["heartbeat_interval_seconds"] = 1
        worker._submit(task)
        assert started.wait(timeout=5)
        state = worker._running[task["id"]]
        deadline = time.monotonic() + 5
        while not state.lease_lost and time.monotonic() < deadline:
            time.sleep(0.01)
        assert state.lease_lost
        deadline = time.monotonic() + 5
        while worker._inflight_count() and time.monotonic() < deadline:
            time.sleep(0.01)

        assert not any(path.endswith("/complete") or path.endswith("/fail") for path, _ in calls)
        assert state.heartbeat_thread is not None and not state.heartbeat_thread.is_alive()
    finally:
        worker._pool.shutdown(wait=True)


def test_long_task_renews_through_more_than_one_default_lease(
    client, account, worker_client, workflow_factory, monkeypatch
):
    import time

    from app.config import settings
    from app.services import worker_service
    from app.worker.registry import Handler, TaskRegistry
    from app.worker.runtime import Worker

    assert settings.lease_seconds == 30
    monkeypatch.setattr(worker_service, "heartbeat_interval", lambda: 5)
    workflow = workflow_factory(
        account["headers"],
        {
            "name": "Worker lease heartbeat integration",
            "description": "A task runs beyond the default lease while heartbeats renew it.",
            "default_max_parallel": 1,
            "steps": [
                {
                    "id": "long",
                    "type": "test.long_running",
                    "input": {},
                    "depends_on": [],
                    "retries": 0,
                    "timeout_seconds": 90,
                }
            ],
        },
    )
    started = client.post(
        f"/api/v1/workflows/{workflow['id']}/runs",
        json={"input": {}},
        headers=account["headers"],
    )
    assert started.status_code == 201, started.text
    began = threading.Event()
    heartbeat_count = 0
    heartbeat_lock = threading.Lock()

    class Api:
        timeout = 30.0

        def post(self, path, body=None, *, timeout=None):
            nonlocal heartbeat_count
            if path.endswith("/heartbeat"):
                with heartbeat_lock:
                    heartbeat_count += 1
            response = worker_client["client"].post(path, json=body)
            assert response.status_code < 400, response.text
            return response.json()

    def long_handler(_payload, context):
        began.set()
        time.sleep(31)
        assert not context.cancelled()
        return {"elapsed_seconds": 31}

    registry = TaskRegistry()
    registry.add(Handler(task_type="test.long_running", func=long_handler))
    worker = Worker(
        token=worker_client["token"],
        worker_id=worker_client["worker_id"],
        registry=registry,
        poll_seconds=0.01,
    )
    worker.api = Api()
    worker.heartbeat_interval_seconds = 5
    try:
        worker.register()
        assert worker._claim_once() == 1
        assert began.wait(timeout=5)
        deadline = time.monotonic() + 50
        while worker._inflight_count() and time.monotonic() < deadline:
            time.sleep(0.05)

        detail_response = client.get(f"/api/v1/runs/{started.json()['id']}", headers=account["headers"])
        assert detail_response.status_code == 200, detail_response.text
        detail = detail_response.json()
        assert not worker._inflight_count()
        assert detail["status"] == "succeeded"
        assert detail["steps"][0]["status"] == "succeeded"
        assert detail["steps"][0]["attempts"] == 1
        assert detail["steps"][0]["output"]["elapsed_seconds"] == 31
        assert heartbeat_count >= 6
        assert not any(step["error"] and step["error"].get("code") == "lease_expired" for step in detail["steps"])
    finally:
        worker.shutdown()
