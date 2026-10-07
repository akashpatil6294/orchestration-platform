"""Embedded worker: run the claim/execute loop inside the API process.

In development the API lifespan starts an in-process worker so a run executes
end to end with no separate worker terminal. The embedded worker reuses
``app.worker.runtime.Worker`` unchanged: it talks to the *same* FastAPI app
object through an in-process ASGI transport, so registration, claims, leases,
heartbeats, completion and shutdown all travel the exact production code path
(auth, routers, services). The only difference is the transport.

The worker credential is generated at startup, stored as a hash in the
``workers`` table, held in memory, and never logged or written to disk.
"""
from __future__ import annotations

import asyncio
import socket
import threading
from typing import Any

import httpx

from app.config import settings
from app.core.logging import get_logger
from app.core.security import generate_token, hash_token, token_prefix
from app.database import SessionLocal
from app.models.worker import Worker as WorkerRow
from app.worker.runtime import ApiClient, ApiError, Worker

logger = get_logger("app.worker.embedded")


class EmbeddedApiClient(ApiClient):
    """An ``ApiClient`` that calls the in-process FastAPI app via ASGI.

    No socket is opened; requests run through the real middleware, routers and
    services in this process. Errors are surfaced as ``ApiError`` exactly like
    the HTTP client so ``Worker`` needs no special-casing.
    """

    def __init__(self, app: Any, token: str, *, timeout: float = 30.0) -> None:
        super().__init__(base_url="internal://embedded", token=token, timeout=timeout)
        self._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://embedded", timeout=timeout)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _arequest(self, method: str, path: str, body: dict[str, Any] | None, timeout: float | None) -> httpx.Response:
        if method == "POST":
            return await self._client.post(path, json=body, headers=self._headers(), timeout=timeout or self.timeout)
        return await self._client.get(path, headers=self._headers(), timeout=timeout or self.timeout)

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        # The worker drives this client from plain threads; bridge the async
        # ASGI transport with a fresh event loop per call.
        try:
            response = asyncio.run(self._arequest(method, path, body, timeout))
        except httpx.TimeoutException as exc:
            raise ApiError(0, "Request to the orchestrator API timed out") from exc
        except httpx.TransportError as exc:
            raise ApiError(0, f"Cannot reach the orchestrator API: {exc}") from exc
        if response.status_code >= 400:
            try:
                error = response.json().get("error", {})
                message = error.get("message") or response.text
                code = error.get("code")
            except ValueError:
                message, code = response.text, None
            raise ApiError(response.status_code, message, code=code)
        return response.json() if response.content else {}

    def post(self, path: str, body: dict[str, Any] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        return self._request("POST", path, body, timeout)

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def get_bytes(self, path: str) -> bytes:
        """Fetch raw bytes (e.g. a document) through the in-process transport."""

        async def _aget() -> httpx.Response:
            return await self._client.get(path, headers=self._headers(), timeout=self.timeout)

        try:
            response = asyncio.run(_aget())
        except httpx.TimeoutException as exc:
            raise ApiError(0, "Request to the orchestrator API timed out") from exc
        except httpx.TransportError as exc:
            raise ApiError(0, f"Cannot reach the orchestrator API: {exc}") from exc
        if response.status_code >= 400:
            try:
                error = response.json().get("error", {})
                message = error.get("message") or response.text
                code = error.get("code")
            except ValueError:
                message, code = response.text, None
            raise ApiError(response.status_code, message, code=code)
        return response.content


def embedded_worker_id() -> str:
    """Distinct per process and per instance so workers never share an identity."""
    import os
    import secrets

    return f"embedded-{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(3)}"


class EmbeddedWorker:
    """Lifecycle handle for the in-process worker thread."""

    def __init__(self, app: Any) -> None:
        token = generate_token("orch")
        worker_id = embedded_worker_id()
        with SessionLocal() as db:
            row = db.get(WorkerRow, worker_id)
            if row is None:
                row = WorkerRow(id=worker_id, name=worker_id)
                db.add(row)
            row.token_hash = hash_token(token)
            row.token_prefix = token_prefix(token)
            row.active = True
            row.max_concurrency = settings.embedded_worker_concurrency
            db.commit()
        # The token lives only here, in memory. It is never logged.
        self.worker_id = worker_id
        self._worker = Worker(
            api_client=EmbeddedApiClient(app, token),
            worker_id=worker_id,
            max_concurrency=settings.embedded_worker_concurrency,
            queues=settings.embedded_worker_queue_list,
        )
        self._thread = threading.Thread(
            target=self._worker.run,
            kwargs={"install_signals": False},
            name="embedded-worker",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        logger.info("Embedded worker started", extra={"worker_id": self.worker_id})

    def stop(self, timeout: float = 30.0) -> None:
        """Signal drain, wait for in-flight tasks, then release the rest."""
        self._worker.stop()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning("Embedded worker thread did not stop in time", extra={"worker_id": self.worker_id})
        # Safety net: the worker row must never leak as active (a stale active
        # row pollutes capacity reporting and the dashboard attention queue).
        # The graceful path above already drains and releases; this only covers
        # a thread that did not finish in time.
        from app.services import worker_service

        with SessionLocal() as db:
            row = db.get(WorkerRow, self.worker_id)
            if row is not None and row.active:
                worker_service.set_worker_active(db, row, False)
                db.commit()
                logger.info("Embedded worker force-deactivated on stop", extra={"worker_id": self.worker_id})


def start_embedded_worker(app: Any) -> EmbeddedWorker | None:
    """Start the embedded worker if enabled; return the handle or None."""
    if not settings.embedded_worker_on:
        logger.info("Embedded worker disabled")
        return None
    handle = EmbeddedWorker(app)
    handle.start()
    return handle


# --------------------------------------------------------------------------- #
# On-demand worker manager
#
# ``POST /api/v1/ops/embedded-worker/start`` creates an embedded worker
# outside the lifespan (e.g. in production where the lifespan worker is off).
# The handle is retained here so ``/stop`` can shut it down gracefully and the
# API lifespan can stop it on process exit. Start is idempotent: asking twice
# returns the running worker instead of spawning a second one.
# --------------------------------------------------------------------------- #

_on_demand_lock = threading.Lock()
_on_demand: EmbeddedWorker | None = None


def on_demand_status() -> dict[str, Any]:
    """Report the on-demand worker's state (no handle details leak)."""
    with _on_demand_lock:
        handle = _on_demand
    if handle is None:
        return {"running": False, "worker_id": None}
    alive = handle._thread.is_alive()
    return {"running": alive, "worker_id": handle.worker_id if alive else None}


def start_on_demand_worker(app: Any) -> EmbeddedWorker | None:
    """Start the on-demand embedded worker; return None when disabled.

    Idempotent: if an on-demand worker is already running in this process, it
    is returned unchanged.
    """
    if not settings.embedded_worker_on:
        logger.info("On-demand embedded worker refused: disabled by configuration")
        return None
    with _on_demand_lock:
        global _on_demand
        if _on_demand is not None and _on_demand._thread.is_alive():
            logger.info("On-demand embedded worker already running", extra={"worker_id": _on_demand.worker_id})
            return _on_demand
        handle = EmbeddedWorker(app)
        handle.start()
        _on_demand = handle
        return handle


def stop_on_demand_worker(timeout: float = 30.0) -> bool:
    """Gracefully stop the on-demand worker; True if one was running."""
    with _on_demand_lock:
        global _on_demand
        handle, _on_demand = _on_demand, None
    if handle is None:
        return False
    logger.info("Stopping on-demand embedded worker", extra={"worker_id": handle.worker_id})
    handle.stop(timeout=timeout)
    return True


__all__ = [
    "EmbeddedApiClient",
    "EmbeddedWorker",
    "embedded_worker_id",
    "on_demand_status",
    "start_embedded_worker",
    "start_on_demand_worker",
    "stop_on_demand_worker",
]
