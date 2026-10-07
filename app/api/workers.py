"""Worker protocol and worker administration.

The protocol endpoints are authenticated with a worker token. The management
endpoints are authenticated with a user session and only expose non-secret
worker metadata — never lease tokens.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi.responses import Response as FastAPIResponse
from fastapi import APIRouter, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUser, CurrentWorker, DbSession, RegisteringWorker
from app.config import settings
from app.core.errors import Conflict, NotFound
from app.models.worker import Worker
from app.schemas.worker import (
    ClaimRequest,
    ClaimResponse,
    CompleteRequest,
    FailRequest,
    HeartbeatRequest,
    HeartbeatResponse,
    TaskResultResponse,
    WorkerCreateRequest,
    WorkerCreateResponse,
    WorkerRegisterRequest,
    WorkerRegisterResponse,
    WorkerShutdownResponse,
    WorkerView,
)
from app.services import worker_service

router = APIRouter(prefix="/api/v1/workers", tags=["workers"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.post("/register", response_model=WorkerRegisterResponse, status_code=status.HTTP_201_CREATED)
def register(payload: WorkerRegisterRequest, worker: RegisteringWorker, db: DbSession) -> dict:
    """Idempotent registration; safe to call on every worker start.

    A worker that stopped gracefully was deactivated by the shutdown endpoint,
    so registration deliberately accepts an inactive worker and brings it back
    online.
    """
    worker = worker_service.register_worker(
        db,
        worker_id=payload.worker_id,
        task_types=payload.task_types,
        queues=payload.queues,
        name=payload.name,
        max_concurrency=payload.max_concurrency,
        metadata=payload.metadata,
    )
    if worker.id != payload.worker_id:
        raise Conflict("Worker id does not match the authenticated credential", code="worker_id_mismatch")
    db.commit()
    db.refresh(worker)
    return {
        "worker_id": worker.id,
        "active": worker.active,
        "task_types": list(worker.task_types or []),
        "queues": list(worker.queues or ["default"]),
        "lease_seconds": settings.lease_seconds,
        "heartbeat_interval_seconds": worker_service.heartbeat_interval(),
        "server_time": _now(),
    }


@router.post("/claim", response_model=ClaimResponse)
def claim(payload: ClaimRequest, worker: CurrentWorker, db: DbSession) -> dict:
    """Claim ready work. Returns ``task: null`` when nothing is available."""
    if payload.worker_id != worker.id:
        raise Conflict("Claim requests must use the authenticated worker id", code="worker_id_mismatch")
    redis_client, stream_cursor = _dispatch_stream_cursor() if payload.wait_seconds > 0 else (None, None)
    deadline = time.monotonic() + payload.wait_seconds
    result = None
    first = True
    try:
        while True:
            result = worker_service.claim_task(
                db,
                worker=worker,
                available_slots=payload.available_slots,
                task_types=payload.task_types,
                queues=payload.queues,
                known_task_ids=payload.in_flight_task_ids,
                record_heartbeat=first,
            )
            first = False
            db.commit()  # release all row locks while waiting for dispatch
            if result or payload.wait_seconds <= 0 or time.monotonic() >= deadline:
                break
            remaining = max(0.0, deadline - time.monotonic())
            if redis_client is not None:
                try:
                    events = redis_client.xread(
                        {settings.outbox_stream: stream_cursor or "0-0"},
                        count=100,
                        block=max(1, int(remaining * 1000)),
                    )
                    if events and events[0][1]:
                        stream_cursor = events[0][1][-1][0]
                except Exception:
                    redis_client.close()
                    redis_client = None
            else:
                time.sleep(min(0.5, remaining))
    finally:
        if redis_client is not None:
            redis_client.close()
    tasks = (result or {}).get("tasks", [])
    return {
        "task": tasks[0] if tasks else None,
        "tasks": tasks,
        "server_time": _now(),
        "retry_after_seconds": max(0.5, min(5.0, settings.worker_poll_seconds)),
        "cancelled_run_ids": [],
    }


def _dispatch_stream_cursor():
    """Return a Redis stream reader positioned at the current tail, if enabled."""
    if not settings.redis_url:
        return None, None
    try:
        import redis

        client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=max(5, settings.worker_long_poll_seconds + 5),
        )
        tail = client.xrevrange(settings.outbox_stream, count=1)
        return client, tail[0][0] if tail else "0-0"
    except Exception:
        return None, None


@router.get("/documents/{document_id}")
def worker_document(document_id: str, worker: CurrentWorker, db: DbSession) -> FastAPIResponse:
    """Fetch a document's bytes for a task this worker currently holds.

    The worker is authorized iff at least one ``running`` step assigned to it
    (valid lease) references the document ID in its step input or its run's
    workflow input. Documents belonging to other owners' tasks are unreachable.
    """

    from app.models.document import Document
    from app.models.run import StepRun, WorkflowRun
    from app.services import document_service

    now = _now()
    held = (
        db.execute(
            select(StepRun, WorkflowRun)
            .join(WorkflowRun, WorkflowRun.id == StepRun.run_id)
            .where(
                StepRun.worker_id == worker.id,
                StepRun.status == "running",
                StepRun.lease_expires_at > now,
            )
        ).all()
    )
    authorized = False
    for step, run in held:
        if _references_document(step.input_data, document_id) or _references_document(run.input_data, document_id):
            authorized = True
            break
    if not authorized:
        raise NotFound("Document not found", code="document_not_found")
    document = db.scalar(select(Document).where(Document.id == document_id))
    if document is None:
        raise NotFound("Document not found", code="document_not_found")
    data = document_service.open_bytes(document)
    return FastAPIResponse(content=data, media_type="application/octet-stream")


def _references_document(node: Any, document_id: str) -> bool:
    """Deep scan for a ``document_id`` value matching the requested document."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "document_id" and value == document_id:
                return True
            if _references_document(value, document_id):
                return True
    elif isinstance(node, list):
        return any(_references_document(item, document_id) for item in node)
    return False


@router.post("/{worker_id}/shutdown", response_model=WorkerShutdownResponse)
def shutdown(worker_id: str, worker: CurrentWorker, db: DbSession) -> dict:
    """Graceful shutdown: release in-flight work so another worker can pick it up."""
    if worker_id != worker.id:
        raise Conflict("You may only shut down your own worker", code="worker_id_mismatch")
    released = worker_service.set_worker_active(db, worker, False)
    db.commit()
    return {
        "worker_id": worker.id,
        "active": False,
        "released_tasks": released,
        "message": "Worker deactivated; in-flight tasks were released for retry",
    }


# --------------------------------------------------------------------------- #
# User-facing worker management
# --------------------------------------------------------------------------- #
@router.post("", response_model=WorkerCreateResponse, status_code=status.HTTP_201_CREATED)
def create_worker(payload: WorkerCreateRequest, user: CurrentUser, db: DbSession) -> dict:
    """Mint a worker credential (admin only).

    Mirrors ``python -m app.cli create-worker`` for admins working from the UI.
    The plaintext token is returned exactly once — the admin explicitly asked
    for it — and only its hash/prefix is stored. It is never logged.
    """
    from app.core.auth import require_admin
    from app.core.security import generate_token, hash_token, token_prefix

    require_admin(user)
    token = generate_token("wrk")
    worker = db.get(Worker, payload.worker_id)
    if worker is None:
        worker = Worker(id=payload.worker_id, name=payload.name or payload.worker_id)
        db.add(worker)
    worker.name = payload.name or worker.name or payload.worker_id
    worker.token_hash = hash_token(token)
    worker.token_prefix = token_prefix(token)
    worker.active = True
    worker.max_concurrency = payload.max_concurrency
    worker.task_types = list(payload.task_types)
    worker.queues = list(payload.queues or ["default"])
    worker.owner_id = user.id
    db.commit()
    db.refresh(worker)
    return {
        "worker_id": worker.id,
        "token": token,
        "token_prefix": worker.token_prefix,
        "active": worker.active,
        "task_types": list(worker.task_types or []),
        "queues": list(worker.queues or ["default"]),
        "max_concurrency": worker.max_concurrency,
    }


@router.get("", response_model=list[WorkerView])
def list_workers(user: CurrentUser, db: DbSession, include_inactive: bool = Query(default=True)) -> list[dict]:
    workers = worker_service.list_workers(db, user.id)
    if not include_inactive:
        workers = [worker for worker in workers if worker.active]
    return [worker_service.worker_view(db, worker) for worker in workers]


@router.post("/{worker_id}/activate", response_model=WorkerView)
def activate(worker_id: str, user: CurrentUser, db: DbSession) -> dict:
    worker = db.get(Worker, worker_id)
    if worker is None or worker.owner_id not in (None, user.id):
        raise NotFound("Worker not found", code="worker_not_found")
    worker_service.set_worker_active(db, worker, True)
    db.commit()
    db.refresh(worker)
    return worker_service.worker_view(db, worker)


@router.post("/{worker_id}/deactivate", response_model=WorkerView)
def deactivate(worker_id: str, user: CurrentUser, db: DbSession) -> dict:
    worker = db.get(Worker, worker_id)
    if worker is None or worker.owner_id not in (None, user.id):
        raise NotFound("Worker not found", code="worker_not_found")
    worker_service.set_worker_active(db, worker, False)
    db.commit()
    db.refresh(worker)
    return worker_service.worker_view(db, worker)


@router.get("/{worker_id}/tasks")
def worker_tasks(worker_id: str, user: CurrentUser, db: DbSession) -> dict:
    """What this worker is executing right now (no lease tokens exposed)."""
    from app.models.run import StepRun

    worker = db.get(Worker, worker_id)
    if worker is None or worker.owner_id not in (None, user.id):
        raise NotFound("Worker not found", code="worker_not_found")
    rows = db.scalars(select(StepRun).where(StepRun.worker_id == worker_id, StepRun.status == "running")).all()
    return {
        "worker_id": worker_id,
        "items": [
            {
                "step_run_id": row.id,
                "run_id": row.run_id,
                "step_key": row.step_key,
                "task_type": row.task_type,
                "attempt": row.attempts,
                "deadline_at": row.deadline_at,
                "started_at": row.started_at,
            }
            for row in rows
        ],
    }


__all__ = ["router"]
