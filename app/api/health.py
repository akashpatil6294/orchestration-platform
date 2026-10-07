"""Health, readiness, metrics and the operator view.

``/health`` is a liveness probe (the process is up), ``/ready`` is a readiness
probe (the database is reachable), and ``/metrics`` renders Prometheus text.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Response
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.config import settings
from app.core.metrics import gauge, render_prometheus, snapshot
from app.database import check_database, database_backend
from app.models.base import utc as ensure_utc
from app.models.run import OutboxMessage, StepRun, WorkflowRun
from app.models.schedule import WorkflowSchedule
from app.models.worker import Worker
from app.services import outbox_service, scheduler_service, worker_service

router = APIRouter(tags=["operations"])
VERSION = "1.0.0"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "version": VERSION, "service": "orchestration-platform"}


@router.get("/ready")
def ready(response: Response, db: DbSession) -> dict:
    ok, error = check_database()
    if not ok:
        response.status_code = 503
        return {
            "status": "degraded",
            "database": database_backend(),
            "workers": {"active": 0},
            "error": error,
            "checked_at": _now(),
        }
    stale_cutoff = _now() - timedelta(seconds=settings.worker_stale_seconds)
    stale_cutoff_utc = ensure_utc(stale_cutoff)
    active_workers = 0
    try:
        for worker in db.scalars(select(Worker).where(Worker.active.is_(True))).all():
            last_seen = ensure_utc(worker.last_seen_at)
            if last_seen is not None and stale_cutoff_utc is not None and last_seen >= stale_cutoff_utc:
                active_workers += 1
    except Exception:
        active_workers = 0
    # No worker is degraded, not failed: the API serves traffic, but queued
    # steps have nothing to execute them.
    status = "ready" if active_workers > 0 else "degraded"
    return {
        "status": status,
        "database": database_backend(),
        "workers": {"active": active_workers},
        "error": error,
        "checked_at": _now(),
    }


@router.get("/metrics")
def metrics(response: Response, db: DbSession) -> Response:
    if not settings.metrics_enabled:
        return Response(content="# metrics disabled\n", media_type="text/plain; version=0.0.4")
    from app.services.ops_metrics import refresh_operational_gauges

    refresh_operational_gauges(db)
    return Response(content=render_prometheus(), media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/api/v1/ops/overview")
def ops_overview(user: CurrentUser, db: DbSession) -> dict:
    """Operational snapshot: queue depth, worker fleet, scheduler health."""
    from app.models.event import EventType, RunEvent

    stale_cutoff = _now() - timedelta(seconds=settings.worker_stale_seconds)
    workers = list(db.scalars(select(Worker).order_by(Worker.last_seen_at.desc())).all())
    stale_cutoff_utc = ensure_utc(stale_cutoff)
    active = [
        worker
        for worker in workers
        if worker.active
        and (last_seen_at := ensure_utc(worker.last_seen_at)) is not None
        and stale_cutoff_utc is not None
        and last_seen_at >= stale_cutoff_utc
    ]
    step_counts = {
        status: int(count)
        for status, count in db.execute(select(StepRun.status, func.count()).group_by(StepRun.status)).all()
    }
    run_counts = {
        status: int(count)
        for status, count in db.execute(select(WorkflowRun.status, func.count()).group_by(WorkflowRun.status)).all()
    }
    task_types: dict[str, int] = {}
    for worker in active:
        for task_type in worker.task_types or []:
            task_types[task_type] = task_types.get(task_type, 0) + 1
    recent_failures = list(
        db.scalars(
            select(RunEvent)
            .where(RunEvent.level.in_(("error", "warning")), RunEvent.created_at >= _now() - timedelta(hours=24))
            .order_by(RunEvent.seq.desc())
            .limit(20)
        ).all()
    )
    backlog = outbox_service.pending_count(db)
    gauge("orchestrator_outbox_backlog", float(backlog))
    gauge("orchestrator_workers_active", float(len(active)))
    return {
        "environment": settings.environment,
        "version": VERSION,
        "server_time": _now(),
        "configuration": settings.as_public_dict(),
        "database": database_backend(),
        "dispatch": {
            "backend": "redis-outbox" if settings.redis_url else "database-polling",
            "redis_configured": bool(settings.redis_url),
            "relay_running": outbox_service.relay.running,
            "outbox_backlog": backlog,
        },
        "scheduler": {
            "enabled": settings.scheduler_enabled,
            "loop_running": scheduler_service.loop.running,
            "interval_seconds": settings.scheduler_interval_seconds,
            "last_tick_at": scheduler_service.loop.last_tick_at,
            "last_error": scheduler_service.loop.last_error,
            "enabled_schedules": int(
                db.scalar(select(func.count()).select_from(WorkflowSchedule).where(WorkflowSchedule.enabled.is_(True))) or 0
            ),
            "due_schedules": len(scheduler_service.due_schedules(db, limit=1000)),
        },
        "workers": {
            "total": len(workers),
            "active": len(active),
            "stale": len(workers) - len(active),
            "task_type_coverage": task_types,
            "items": [worker_service.worker_view(db, worker) for worker in workers[:25]],
        },
        "runs": {"by_status": run_counts, "total": sum(run_counts.values())},
        "steps": {"by_status": step_counts, "total": sum(step_counts.values())},
        "recent_failures": [
            {
                "seq": event.seq,
                "run_id": event.run_id,
                "step_key": event.step_key,
                "type": event.event_type,
                "level": event.level,
                "message": event.message,
                "at": event.created_at,
            }
            for event in recent_failures
        ],
        "metrics": snapshot(),
    }


@router.post("/api/v1/ops/maintenance/recover-leases")
def recover_leases(user: CurrentUser, db: DbSession) -> dict:
    """Operator action: reclaim work whose worker stopped reporting."""
    recovered = worker_service.recover_expired_leases(db)
    db.commit()
    return {"recovered": recovered}


@router.post("/api/v1/ops/maintenance/prune-outbox")
def prune_outbox(user: CurrentUser, db: DbSession) -> dict:
    removed = outbox_service.prune_published(db)
    db.commit()
    return {"removed": removed}


@router.post("/api/v1/ops/maintenance/scheduler-tick")
def scheduler_tick(user: CurrentUser, db: DbSession) -> dict:
    started = scheduler_service.tick(db, actor=user.email)
    db.commit()
    return {"runs_started": started}


__all__ = ["VERSION", "router"]
