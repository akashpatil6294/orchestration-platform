"""Worker runtime: claim, execute, heartbeat, report, shut down gracefully.

Execution model
---------------
A small thread pool runs claimed tasks. Each task gets a daemon heartbeat thread
that renews its own lease while the handler runs; a separate maintenance thread
refreshes the worker registration while the process is idle.

Shutdown
--------
``SIGINT``/``SIGTERM`` set a stop flag: the worker stops claiming, waits up to
``shutdown_grace_seconds`` for in-flight tasks to finish, then reports itself
inactive so the API releases anything still held for another worker to retry.
Handlers never observe a half-reported result: a task is only reported after its
handler returns or raises.
"""
from __future__ import annotations

import json
import signal
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.core.logging import get_logger, log_extra
from app.core.security import redact
from app.worker.registry import TaskContext, TaskRegistry, UnsupportedTaskType, registry as default_registry

logger = get_logger("app.worker")


class ApiError(Exception):
    def __init__(self, status: int, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code

    @property
    def ownership_lost(self) -> bool:
        return self.status in {401, 403, 404, 409} or self.code in {
            "deadline_exceeded",
            "invalid_lease_token",
            "lease_expired",
            "lease_lost",
            "task_not_assigned",
            "task_not_found",
            "worker_inactive",
            "worker_id_mismatch",
        }


class Cancelled(Exception):
    """Raised by a handler when the run was cancelled mid-execution."""


def classify_exception(exc: Exception) -> bool:
    """Classify handler failures while allowing task handlers to override policy."""
    explicit = getattr(exc, "retryable", None)
    if isinstance(explicit, bool):
        return explicit
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None) or getattr(exc, "status", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None and isinstance(exc, urllib.error.HTTPError):
        status = exc.code
    if isinstance(status, int) and 400 <= status < 600:
        return status in {408, 409, 425, 429} or status >= 500
    if isinstance(exc, (ValueError, TypeError, KeyError, UnsupportedTaskType)):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError, urllib.error.URLError)):
        return True
    return True


class ApiClient:
    """Minimal JSON client for the worker protocol."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout if timeout is not None else self.timeout) as response:
                payload = response.read()
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(detail)
                error = parsed.get("error", {})
                message = error.get("message") or detail
                code = error.get("code")
            except (ValueError, AttributeError):
                message = detail
                code = None
            raise ApiError(exc.code, message, code=code) from exc
        except urllib.error.URLError as exc:
            raise ApiError(0, f"Cannot reach the orchestrator API at {self.base_url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ApiError(0, "Request to the orchestrator API timed out") from exc
        except ConnectionError as exc:
            raise ApiError(0, "Connection to the orchestrator API was interrupted") from exc

    def get(self, path: str) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(f"{self.base_url}{path}", headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, exc.read().decode("utf-8", "replace")) from exc
        except urllib.error.URLError as exc:
            raise ApiError(0, f"Cannot reach the orchestrator API: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ApiError(0, "Request to the orchestrator API timed out") from exc
        except ConnectionError as exc:
            raise ApiError(0, "Connection to the orchestrator API was interrupted") from exc

    def get_bytes(self, path: str) -> bytes:
        """Fetch raw bytes (e.g. a document) through the worker credential."""
        headers = {"Accept": "application/octet-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(f"{self.base_url}{path}", headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, exc.read().decode("utf-8", "replace")) from exc
        except urllib.error.URLError as exc:
            raise ApiError(0, f"Cannot reach the orchestrator API: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ApiError(0, "Request to the orchestrator API timed out") from exc
        except ConnectionError as exc:
            raise ApiError(0, "Connection to the orchestrator API was interrupted") from exc


@dataclass
class RunningTask:
    """State shared between a task thread and the heartbeat thread."""

    task_id: str
    step_key: str
    run_id: str
    attempt: int
    lease_token: str
    task: dict[str, Any]
    idempotency_key: str
    secret_values: tuple[str, ...] = field(default=(), repr=False)
    deadline_at: Any = None
    api_client: Any = None
    cancel_requested: bool = False
    lease_lost: bool = False
    finished: bool = False
    logs: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    heartbeat_stop: threading.Event = field(default_factory=threading.Event)
    heartbeat_thread: threading.Thread | None = None

    def log(self, message: str, **fields: Any) -> None:
        safe_message = _scrub_secrets(message, self.secret_values)
        safe_fields = _scrub_secrets({key: value for key, value in fields.items() if value is not None}, self.secret_values)
        entry = {"level": "info", "message": safe_message, "step_key": self.step_key, "at": _iso_now()}
        entry.update(safe_fields)
        with self.lock:
            self.logs.append(entry)
        logger.info(safe_message, extra={"step_key": self.step_key, "run_id": self.run_id, **safe_fields})

    def snapshot_logs(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.logs)

    def cancelled(self) -> bool:
        return self.cancel_requested


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _secret_values(input_data: Any, paths: list[str]) -> tuple[str, ...]:
    import re

    values: set[str] = set()
    for path in paths:
        node = input_data
        parts = re.findall(r"[^.\[\]]+|\[\d+\]", path)
        try:
            for part in parts:
                node = node[int(part[1:-1])] if part.startswith("[") else node[part]
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if isinstance(node, str) and node:
            values.add(node)
    return tuple(sorted(values, key=len, reverse=True))


def _scrub_secrets(value: Any, secret_values: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return redact({key: _scrub_secrets(item, secret_values) for key, item in value.items()})
    if isinstance(value, list):
        return [_scrub_secrets(item, secret_values) for item in value]
    if isinstance(value, str):
        result = value
        for secret in secret_values:
            if len(secret) >= 4:
                result = result.replace(secret, "[REDACTED]")
            elif result == secret:
                result = "[REDACTED]"
        return result
    return redact(value)


class Worker:
    """A polling worker process."""

    def __init__(
        self,
        *,
        api_base: str | None = None,
        token: str | None = None,
        worker_id: str | None = None,
        task_types: list[str] | None = None,
        max_concurrency: int | None = None,
        queues: list[str] | None = None,
        registry: TaskRegistry | None = None,
        poll_seconds: float | None = None,
        api_client: ApiClient | None = None,
    ) -> None:
        self.registry = registry or default_registry
        self.api = (
            api_client
            if api_client is not None
            else ApiClient(api_base or settings.api_base, token if token is not None else settings.worker_token)
        )
        self.worker_id = worker_id or settings.worker_id
        self.task_types = task_types if task_types is not None else (settings.worker_task_type_list or self.registry.types())
        self.queues = queues if queues is not None else settings.worker_queue_list
        self.max_concurrency = max_concurrency or settings.worker_max_concurrency
        self.poll_seconds = poll_seconds or settings.worker_poll_seconds
        self.shutdown_grace_seconds = max(5, settings.lease_seconds)

        # Chaos mode (Stage H5): opt-in failure injection via CHAOS_* env vars.
        from app.worker.chaos import ChaosConfig

        self._chaos = ChaosConfig()
        if self._chaos.enabled:
            import logging

            logging.getLogger("app.worker").warning("CHAOS MODE ENABLED: %s", self._chaos.describe())

        # Guard every DNS resolution in this process (including the HTTP
        # client's connect-time lookups) so a rebinding host cannot slip a
        # private address past the per-hop connector checks. The orchestrator
        # API itself is exempt: workers legitimately connect to it, often on
        # loopback.
        from urllib.parse import urlsplit as _urlsplit

        from app.worker.connectors import install_resolver_guard

        install_resolver_guard(exempt_hosts={_urlsplit(self.api.base_url).hostname or ""})

        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=self.max_concurrency, thread_name_prefix="task")
        self._running: dict[str, RunningTask] = {}
        self._running_lock = threading.Lock()
        self._futures: set[Future] = set()
        self._heartbeat_thread: threading.Thread | None = None
        self.registered = False
        self.processed = 0
        self.failures = 0
        self.lease_seconds = settings.lease_seconds
        self.heartbeat_interval_seconds = settings.worker_heartbeat_interval

    # ------------------------------------------------------------------ lifecycle
    def register(self) -> dict[str, Any]:
        supported = sorted(set(self.task_types) | set(self.registry.types()))
        response = self.api.post(
            "/api/v1/workers/register",
            {
                "worker_id": self.worker_id,
                "name": f"{socket.gethostname()}-{self.worker_id}",
                "task_types": supported,
                "queues": self.queues,
                "max_concurrency": self.max_concurrency,
                "metadata": {"hostname": socket.gethostname(), "handlers": self.registry.describe()},
            },
        )
        self.lease_seconds = int(response.get("lease_seconds", settings.lease_seconds))
        self.heartbeat_interval_seconds = int(
            response.get("heartbeat_interval_seconds", settings.worker_heartbeat_interval)
        )
        self.registered = True
        logger.info(
            "Worker registered",
            extra={"worker_id": self.worker_id, "task_types": supported, "max_concurrency": self.max_concurrency},
        )
        return response

    def install_signal_handlers(self) -> None:
        def handle(signum: int, _frame: Any) -> None:
            logger.info("Shutdown signal received; finishing in-flight tasks", extra={"signal": signum})
            self.stop()

        for name in ("SIGINT", "SIGTERM"):
            signum = getattr(signal, name, None)
            if signum is not None:
                try:
                    signal.signal(signum, handle)
                except (ValueError, OSError):  # not on the main thread
                    pass

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # ------------------------------------------------------------------ run loop
    def run(self, *, max_iterations: int | None = None, install_signals: bool = True) -> None:
        if install_signals:
            self.install_signal_handlers()
        self.register()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True)
        self._heartbeat_thread.start()
        logger.info("Worker polling for tasks", extra={"worker_id": self.worker_id, "api": self.api.base_url})
        iterations = 0
        try:
            while not self._stop.is_set():
                iterations += 1
                if max_iterations is not None and iterations > max_iterations:
                    break
                try:
                    claimed = self._claim_once()
                except ApiError as exc:
                    if exc.code == "worker_inactive":
                        # An admin deactivated this worker (graceful stop from
                        # the outside). Exit the loop instead of spinning on
                        # 403s; shutdown() releases nothing further.
                        logger.info("Worker deactivated remotely; stopping", extra={"worker_id": self.worker_id})
                        break
                    logger.warning("Claim failed", extra={"status": exc.status, "error": exc.message})
                    claimed = 0
                if claimed == 0 and settings.worker_long_poll_seconds <= 0:
                    self._stop.wait(self.poll_seconds)
        finally:
            self.shutdown()

    def _claim_once(self) -> int:
        slots = self.max_concurrency - self._inflight_count()
        if slots <= 0:
            self._stop.wait(self.poll_seconds)
            return 0
        with self._running_lock:
            in_flight_task_ids = sorted(self._running)
        response = self.api.post(
            "/api/v1/workers/claim",
            {
                "worker_id": self.worker_id,
                "available_slots": slots,
                "task_types": sorted(self.registry.types()),
                "queues": self.queues,
                "in_flight_task_ids": in_flight_task_ids,
                "wait_seconds": settings.worker_long_poll_seconds,
            },
        )
        tasks = response.get("tasks") or ([response["task"]] if response.get("task") else [])
        for task in tasks:
            self._submit(task)
        return len(tasks)

    def _inflight_count(self) -> int:
        with self._running_lock:
            return len(self._running)

    def _submit(self, task: dict[str, Any]) -> None:
        state = RunningTask(
            task_id=task["id"],
            step_key=task["step_key"],
            run_id=task["run_id"],
            attempt=task["attempt"],
            lease_token=task["lease_token"],
            task={key: value for key, value in task.items() if key != "lease_token"},
            idempotency_key=task["idempotency_key"],
            secret_values=_secret_values(task.get("input") or {}, task.get("redacted_keys") or []),
            deadline_at=task.get("deadline_at"),
            api_client=self.api,
        )
        with self._running_lock:
            self._running[task["id"]] = state
        interval = int(task.get("heartbeat_interval_seconds") or self.heartbeat_interval_seconds)
        state.heartbeat_thread = threading.Thread(
            target=self._task_heartbeat_loop,
            args=(state, max(1, interval)),
            name=f"lease-heartbeat-{task['id'][:8]}",
            daemon=True,
        )
        state.log("Task heartbeat started", interval_seconds=max(1, interval))
        state.heartbeat_thread.start()
        try:
            future = self._pool.submit(self._execute, task, state)
        except Exception:
            state.finished = True
            self._stop_task_heartbeat(state)
            self._forget(task["id"])
            raise
        with self._running_lock:
            self._futures.add(future)
        future.add_done_callback(self._finish)

    def _finish(self, future: Future) -> None:
        with self._running_lock:
            self._futures.discard(future)

    # ------------------------------------------------------------------ execution
    def _execute(self, task: dict[str, Any], state: RunningTask) -> None:
        try:
            if state.lease_lost:
                return
            task_type = task["type"]
            payload = task.get("input") or {}
            started = time.perf_counter()
            # Chaos injection (Stage H5): opt-in via CHAOS_* env vars.
            try:
                from app.worker.chaos import maybe_inject, should_duplicate

                maybe_inject(self._chaos, task.get("step_key", ""))
            except Exception as chaos_exc:  # noqa: BLE001 - ChaosError or import issue
                from app.worker.chaos import ChaosError

                if isinstance(chaos_exc, ChaosError):
                    self._report_failure(task, state, chaos_exc, retryable=True)
                    return
            try:
                handler = self.registry.resolve(task_type)
            except UnsupportedTaskType as exc:
                self._report_failure(task, state, exc, retryable=False)
                return
            try:
                state.log(f"Starting {task_type} (attempt {state.attempt})", task_type=task_type)
                output = _scrub_secrets(handler.func(payload, state), state.secret_values)
                duration = time.perf_counter() - started
                state.log(f"Finished {task_type} in {duration:.2f}s", task_type=task_type, duration_seconds=round(duration, 3))
                if state.lease_lost:
                    state.log("Task result discarded because the lease was lost", level="warning")
                    return
                self._report_success(task, state, output)
            except Cancelled:
                if not state.lease_lost:
                    self._report_failure(
                        task, state, RuntimeError("Task stopped because the run was cancelled"), retryable=False, cancelled=True
                    )
            except Exception as exc:  # noqa: BLE001 - reported to the platform, not swallowed
                if not state.lease_lost:
                    self._report_failure(task, state, exc, retryable=classify_exception(exc))
        finally:
            state.finished = True
            self._stop_task_heartbeat(state)
            self._forget(state.task_id)

    def _report_success(self, task: dict[str, Any], state: RunningTask, output: Any) -> None:
        try:
            self.api.post(
                f"/api/v1/tasks/{state.task_id}/complete",
                {"worker_id": self.worker_id, "lease_token": state.lease_token, "output": output, "logs": state.snapshot_logs()},
            )
            self.processed += 1
            logger.info("Task completed", extra={"task_id": state.task_id, "step_key": state.step_key, "run_id": state.run_id})
            # Chaos: duplicate delivery exercises server-side idempotency.
            from app.worker.chaos import should_duplicate

            if should_duplicate(self._chaos):
                logger.warning("CHAOS: sending duplicate completion", extra={"task_id": state.task_id})
                try:
                    self.api.post(
                        f"/api/v1/tasks/{state.task_id}/complete",
                        {"worker_id": self.worker_id, "lease_token": state.lease_token, "output": output, "logs": []},
                    )
                except ApiError:
                    pass  # Duplicate rejection is the expected outcome.
        except ApiError as exc:
            # The lease was reclaimed or the run was cancelled; the platform owns
            # the retry, so this is recorded and not retried locally.
            logger.warning("Result rejected by the platform", extra={"task_id": state.task_id, "status": exc.status, "error": exc.message})

    def _report_failure(self, task: dict[str, Any], state: RunningTask, exc: Exception, *, retryable: bool, cancelled: bool = False) -> None:
        self.failures += 1
        error = {
            "type": type(exc).__name__,
            "message": _scrub_secrets(str(exc)[:2000], state.secret_values),
            "retryable": retryable,
            "attempt": state.attempt,
            "cancelled": cancelled,
        }
        state.log(f"Task failed: {error['message']}", level="error")
        try:
            self.api.post(
                f"/api/v1/tasks/{state.task_id}/fail",
                {"worker_id": self.worker_id, "lease_token": state.lease_token, "error": error, "retryable": retryable, "logs": state.snapshot_logs()},
            )
            logger.warning("Task failed", extra={"task_id": state.task_id, "step_key": state.step_key, "error": error["message"]})
        except ApiError as api_exc:
            logger.warning("Failure report rejected by the platform", extra={"task_id": state.task_id, "status": api_exc.status, "error": api_exc.message})

    def _forget(self, task_id: str) -> None:
        with self._running_lock:
            self._running.pop(task_id, None)

    # ------------------------------------------------------------------ heartbeat
    def _heartbeat_loop(self) -> None:
        interval = max(5, settings.worker_stale_seconds // 3)
        while not self._stop.is_set():
            self._stop.wait(interval)
            if self._stop.is_set() or not self.registered:
                continue
            try:
                self.api.post(
                    "/api/v1/workers/register",
                    {
                        "worker_id": self.worker_id,
                        "name": f"{socket.gethostname()}-{self.worker_id}",
                        "task_types": sorted(self.registry.types()),
                        "max_concurrency": self.max_concurrency,
                        "metadata": {"hostname": socket.gethostname(), "inflight": self._inflight_count()},
                    },
                )
            except ApiError as exc:
                logger.warning(
                    "Worker registration refresh failed",
                    extra={"worker_id": self.worker_id, "status": exc.status, "error": exc.message},
                )

    def _task_heartbeat_loop(self, state: RunningTask, interval: int) -> None:
        while not state.heartbeat_stop.wait(interval):
            if state.finished:
                return
            with self._running_lock:
                active_tasks = len(self._running)
            try:
                response = self.api.post(
                    f"/api/v1/tasks/{state.task_id}/heartbeat",
                    {"worker_id": self.worker_id, "lease_token": state.lease_token, "active_tasks": active_tasks},
                    timeout=min(3.0, self.api.timeout),
                )
            except ApiError as exc:
                logger.warning(
                    "Task heartbeat failed",
                    extra={
                        "task_id": state.task_id,
                        "step_key": state.step_key,
                        "status": exc.status,
                        "code": exc.code,
                        "error": exc.message,
                    },
                )
                if exc.ownership_lost:
                    state.lease_lost = True
                    state.cancel_requested = True
                    state.log("Task lease ownership lost; result will not be reported", level="warning")
                    return
                continue
            if state.finished:
                return
            if response.get("cancel_requested"):
                state.cancel_requested = True
                state.log("Cancellation requested by the platform; stopping soon", level="warning")
            else:
                state.log("Task lease renewed", lease_expires_at=response.get("lease_expires_at"))

    def _stop_task_heartbeat(self, state: RunningTask) -> None:
        state.heartbeat_stop.set()
        thread = state.heartbeat_thread
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=min(5.0, self.api.timeout + 0.5))
        if thread.is_alive():
            logger.error(
                "Task heartbeat thread did not stop promptly",
                extra={"task_id": state.task_id, "step_key": state.step_key},
            )
        else:
            state.log("Task heartbeat stopped")

    # ------------------------------------------------------------------ shutdown
    def shutdown(self) -> None:
        """Wait for in-flight work, then hand anything unfinished back."""
        deadline = time.monotonic() + self.shutdown_grace_seconds
        while self._inflight_count() > 0 and time.monotonic() < deadline:
            time.sleep(0.2)
        remaining = self._inflight_count()
        if remaining:
            logger.warning("Shutdown grace elapsed with tasks still running", extra={"inflight": remaining})
        self._pool.shutdown(wait=False, cancel_futures=False)
        if self.registered:
            try:
                self.api.post(f"/api/v1/workers/{self.worker_id}/shutdown", {})
                logger.info("Worker deactivated", extra={"worker_id": self.worker_id})
            except ApiError as exc:
                logger.warning("Could not deactivate worker cleanly", extra={"error": exc.message})
        logger.info(
            "Worker stopped",
            extra=log_extra(worker_id=self.worker_id, processed=self.processed, failures=self.failures),
        )


__all__ = ["ApiClient", "ApiError", "Cancelled", "RunningTask", "Worker"]
