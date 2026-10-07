"""Operator endpoints: queue capacity, worker coverage, embedded worker control.

``GET /api/v1/ops/capacity`` answers "why is my run still queued?" without
leaking other tenants' data: worker information is aggregate, and queued-step
counts are broken down per owner only for admins (everyone else sees their own
counts). ``POST /api/v1/ops/embedded-worker/start`` lets an admin start the
in-process worker on demand when the setting allows it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.config import settings
from app.core.auth import require_admin
from app.models.base import utc as ensure_utc
from app.models.run import StepRun, WorkflowRun
from app.models.worker import Worker
from app.models.workflow import Workflow

router = APIRouter(tags=["operations"])

READY_STEP_STATUSES = ("pending", "queued", "scheduled")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _active_workers(db: DbSession) -> list[Worker]:
    stale_cutoff = ensure_utc(_now() - timedelta(seconds=settings.worker_stale_seconds))
    workers = list(db.scalars(select(Worker).order_by(Worker.last_seen_at.desc())).all())
    return [
        worker
        for worker in workers
        if worker.active
        and (last_seen := ensure_utc(worker.last_seen_at)) is not None
        and stale_cutoff is not None
        and last_seen >= stale_cutoff
    ]


def _worker_view(worker: Worker) -> dict:
    return {
        "id": worker.id,
        "name": worker.name,
        "task_types": list(worker.task_types or []),
        "queues": list(worker.queues or []),
        "max_concurrency": worker.max_concurrency,
        "embedded": worker.id.startswith("embedded-"),
        "last_seen_at": worker.last_seen_at,
    }


@router.get("/api/v1/ops/capacity")
def capacity(user: CurrentUser, db: DbSession) -> dict:
    """Worker coverage vs queued demand. Tenant-safe: no step contents, no cross-owner detail."""
    now = _now()
    workers = _active_workers(db)
    worker_views = [_worker_view(worker) for worker in workers]

    queued = list(
        db.scalars(
            select(StepRun).where(
                StepRun.status.in_(READY_STEP_STATUSES),
                StepRun.available_at <= now,
            )
        ).all()
    )

    # Per-owner queued counts. Admins see every owner; anyone else sees only
    # their own steps. Queue/task-type aggregates are owner-scoped the same
    # way: a non-admin must not learn anything about other tenants' queued
    # demand. Step contents are never exposed to anyone.
    owner_counts: dict[str, int] = {}
    if queued:
        run_ids = {step.run_id for step in queued}
        owner_by_run = {
            run_id: owner_id
            for run_id, owner_id in db.execute(
                select(WorkflowRun.id, Workflow.owner_id)
                .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
                .where(WorkflowRun.id.in_(run_ids))
            ).all()
        }
        step_owner = [(step, owner_by_run.get(step.run_id)) for step in queued]
        if user.is_admin:
            for _, owner_id in step_owner:
                if owner_id:
                    owner_counts[owner_id] = owner_counts.get(owner_id, 0) + 1
        else:
            queued = [step for step, owner_id in step_owner if owner_id == user.id]
            owner_counts[user.id] = len(queued)

    queues: dict[str, dict] = {}
    uncovered_task_types: dict[str, dict] = {}
    for step in queued:
        entry = queues.setdefault(step.queue_name, {"queued_steps": 0, "oldest_queued_age_seconds": 0.0, "task_types": set()})
        entry["queued_steps"] += 1
        created = ensure_utc(step.created_at) or now
        age = max(0.0, (ensure_utc(now) - created).total_seconds()) if ensure_utc(now) else 0.0
        entry["oldest_queued_age_seconds"] = max(entry["oldest_queued_age_seconds"], age)
        entry["task_types"].add(step.task_type)

    for queue_name, entry in queues.items():
        task_types = sorted(entry.pop("task_types"))
        entry["task_types"] = task_types
        for task_type in task_types:
            covered = any(
                task_type in (worker.task_types or []) and queue_name in (worker.queues or [])
                for worker in workers
            )
            info = uncovered_task_types.setdefault(task_type, {"covered": False, "queues": set()})
            info["queues"].add(queue_name)
            if covered:
                info["covered"] = True
    for info in uncovered_task_types.values():
        info["queues"] = sorted(info["queues"])

    return {
        "server_time": now,
        "active_workers": len(workers),
        "workers": worker_views,
        "queues": queues,
        "task_types": uncovered_task_types,
        "queued_owner_counts": owner_counts,
        "embedded_worker_allowed": settings.embedded_worker_on,
        "queue_wait_warning_seconds": settings.queue_wait_warning_seconds,
    }


@router.post("/api/v1/ops/embedded-worker/start")
def start_embedded_worker(user: CurrentUser) -> dict:
    """Start the in-process worker on demand (admin only, setting must allow it).

    Idempotent: if an on-demand worker is already running, its identity is
    returned instead of spawning a second one.
    """
    require_admin(user)
    if not settings.embedded_worker_on:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=409,
            detail="The embedded worker is disabled by configuration (EMBEDDED_WORKER_ENABLED=false)",
        )
    from app.main import app as fastapi_app
    from app.worker import embedded as embedded_module

    handle = embedded_module.start_on_demand_worker(fastapi_app)
    assert handle is not None  # enabled, so a handle is always returned
    return {"worker_id": handle.worker_id, "status": "started"}


@router.post("/api/v1/ops/embedded-worker/stop")
def stop_embedded_worker(user: CurrentUser) -> dict:
    """Gracefully stop the on-demand in-process worker (admin only).

    The worker drains in-flight tasks, releases its leases and deactivates its
    row, exactly like an external worker's graceful shutdown.
    """
    require_admin(user)
    from app.worker import embedded as embedded_module

    stopped = embedded_module.stop_on_demand_worker()
    return {"status": "stopped" if stopped else "not_running"}


@router.get("/api/v1/ops/embedded-worker")
def embedded_worker_status(user: CurrentUser) -> dict:
    """Report the on-demand in-process worker's state (admin only)."""
    require_admin(user)
    from app.worker import embedded as embedded_module

    return embedded_module.on_demand_status()


__all__ = ["router"]
