"""Task lease routes used by workers while a step is running.

Every call must present the lease token issued at claim time. The token is
single-use per attempt: once the attempt is closed (completed, failed or
reclaimed) the token is cleared and further calls are rejected.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from app.api.deps import CurrentUser, CurrentWorker, DbSession
from app.core.errors import Conflict
from app.schemas.worker import (
    CompleteRequest,
    FailRequest,
    HeartbeatRequest,
    HeartbeatResponse,
    TaskResultResponse,
)
from app.services import worker_service

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.post("/{task_id}/heartbeat", response_model=HeartbeatResponse)
def heartbeat(task_id: str, payload: HeartbeatRequest, worker: CurrentWorker, db: DbSession) -> dict:
    """Renew the lease and learn whether the run was cancelled."""
    if payload.worker_id != worker.id:
        raise Conflict("Heartbeats must use the authenticated worker id", code="worker_id_mismatch")
    step, run = worker_service.verify_lease(db, task_id, worker_id=worker.id, lease_token=payload.lease_token)
    result = worker_service.heartbeat(db, step=step, run=run, worker=worker, active_tasks=payload.active_tasks)
    db.commit()
    return {**result, "server_time": _now()}


@router.post("/{task_id}/complete", response_model=TaskResultResponse)
def complete(task_id: str, payload: CompleteRequest, worker: CurrentWorker, db: DbSession) -> dict:
    if payload.worker_id != worker.id:
        raise Conflict("Results must be reported by the worker that claimed the task", code="worker_id_mismatch")
    step, run = worker_service.verify_lease(db, task_id, worker_id=worker.id, lease_token=payload.lease_token)
    result = worker_service.complete_task(db, step=step, run=run, worker=worker, output=payload.output, logs=payload.logs)
    db.commit()
    return {**result, "message": "Result recorded"}


@router.post("/{task_id}/fail", response_model=TaskResultResponse)
def fail(task_id: str, payload: FailRequest, worker: CurrentWorker, db: DbSession) -> dict:
    if payload.worker_id != worker.id:
        raise Conflict("Results must be reported by the worker that claimed the task", code="worker_id_mismatch")
    step, run = worker_service.verify_lease(db, task_id, worker_id=worker.id, lease_token=payload.lease_token)
    result = worker_service.fail_task(
        db, step=step, run=run, worker=worker, error=payload.error, retryable=payload.retryable, logs=payload.logs
    )
    db.commit()
    return {**result, "message": "Failure recorded"}


# --- Builder-facing task-type catalog ----------------------------------------
# The visual builder's palette is driven by this endpoint. It is authenticated
# as the current user (not a worker): anyone who can edit workflows can see it.

task_types_router = APIRouter(prefix="/api/v1/task-types", tags=["task-types"])


@task_types_router.get("", response_model=dict)
def list_task_types(user: CurrentUser) -> dict:
    """List every registered task type with its builder metadata.

    The palette groups by ``category``; the inspector renders ``input_schema``
    as typed fields; the policy section shows ``default_policy`` and warns
    when retries are enabled on a non-``idempotent`` task.
    """
    # Import here so the endpoint reflects the live registry without a
    # hard import cycle at module load.
    from app.worker import handlers as _handlers  # noqa: F401
    from app.worker.registry import registry

    items = registry.describe()
    categories = sorted({item["category"] for item in items if item["category"]})
    return {"items": items, "categories": categories, "count": len(items)}


__all__ = ["router", "task_types_router"]
