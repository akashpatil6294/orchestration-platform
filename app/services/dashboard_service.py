"""Dashboard aggregations.

Everything the landing page shows is computed in a small number of grouped
queries so the page stays fast as history grows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.base import utc as ensure_utc
from app.models.event import EventType, RunEvent
from app.models.run import StepAttempt, StepRun, WorkflowRun
from app.models.schedule import WorkflowSchedule
from app.models.worker import Worker
from app.models.workflow import Workflow

ACTIVE_RUN_STATUSES = ("queued", "running", "cancelling")
TERMINAL_RUN_STATUSES = ("succeeded", "failed", "cancelled")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return round(ordered[index], 3)


def _as_utc(value: datetime) -> datetime:
    normalized = ensure_utc(value)
    if normalized is None:
        raise ValueError("A required timestamp is missing")
    return normalized


def build_dashboard(db: Session, owner_id: str, *, window_hours: int = 24, recent_limit: int = 8) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)

    owned_workflows = select(Workflow.id).where(Workflow.owner_id == owner_id).scalar_subquery()

    status_rows = db.execute(
        select(WorkflowRun.status, func.count())
        .where(WorkflowRun.workflow_id.in_(owned_workflows))
        .group_by(WorkflowRun.status)
    ).all()
    status_counts = {status: int(count) for status, count in status_rows}
    total_runs = sum(status_counts.values())

    window_rows = db.execute(
        select(WorkflowRun.status, func.count())
        .where(WorkflowRun.workflow_id.in_(owned_workflows), WorkflowRun.created_at >= since)
        .group_by(WorkflowRun.status)
    ).all()
    window_counts = {status: int(count) for status, count in window_rows}
    started = sum(window_counts.values())

    finished_rows = db.scalars(
        select(WorkflowRun).where(
            WorkflowRun.workflow_id.in_(owned_workflows),
            WorkflowRun.created_at >= since,
            WorkflowRun.status.in_(TERMINAL_RUN_STATUSES),
        )
    ).all()
    durations = [
        (row.finished_at - row.started_at).total_seconds()
        for row in finished_rows
        if row.started_at is not None and row.finished_at is not None
    ]
    succeeded = sum(1 for row in finished_rows if row.status == "succeeded")
    success_rate = round(succeeded / len(finished_rows), 4) if finished_rows else 0.0

    step_status_rows = db.execute(
        select(StepRun.status, func.count())
        .select_from(StepRun)
        .join(WorkflowRun, WorkflowRun.id == StepRun.run_id)
        .where(WorkflowRun.workflow_id.in_(owned_workflows), WorkflowRun.created_at >= since)
        .group_by(StepRun.status)
    ).all()
    step_counts = {status: int(count) for status, count in step_status_rows}

    attempts = int(
        db.scalar(
            select(func.count())
            .select_from(StepAttempt)
            .join(StepRun, StepRun.id == StepAttempt.step_run_id)
            .join(WorkflowRun, WorkflowRun.id == StepRun.run_id)
            .where(WorkflowRun.workflow_id.in_(owned_workflows), WorkflowRun.created_at >= since)
        )
        or 0
    )
    retried = int(
        db.scalar(
            select(func.count())
            .select_from(StepRun)
            .join(WorkflowRun, WorkflowRun.id == StepRun.run_id)
            .where(
                WorkflowRun.workflow_id.in_(owned_workflows),
                WorkflowRun.created_at >= since,
                StepRun.attempts > 1,
            )
        )
        or 0
    )
    timed_out = int(
        db.scalar(
            select(func.count())
            .select_from(RunEvent)
            .join(WorkflowRun, WorkflowRun.id == RunEvent.run_id)
            .where(
                WorkflowRun.workflow_id.in_(owned_workflows),
                RunEvent.created_at >= since,
                RunEvent.event_type == EventType.STEP_TIMEOUT,
            )
        )
        or 0
    )
    pending_steps = step_counts.get("pending", 0) + step_counts.get("retrying", 0)
    running_steps = step_counts.get("running", 0)

    stale_cutoff = now - timedelta(seconds=settings.worker_stale_seconds)
    workers = list(db.scalars(select(Worker).order_by(Worker.last_seen_at.desc())).all())
    active_workers = sum(
        1
        for worker in workers
        if worker.active and _as_utc(worker.last_seen_at) >= stale_cutoff
    )

    schedules_enabled = int(
        db.scalar(
            select(func.count()).select_from(WorkflowSchedule).where(WorkflowSchedule.owner_id == owner_id, WorkflowSchedule.enabled.is_(True))
        )
        or 0
    )
    schedules_due = int(
        db.scalar(
            select(func.count())
            .select_from(WorkflowSchedule)
            .where(
                WorkflowSchedule.owner_id == owner_id,
                WorkflowSchedule.enabled.is_(True),
                WorkflowSchedule.next_run_at.is_not(None),
                WorkflowSchedule.next_run_at <= utcnow(),
            )
        )
        or 0
    )
    workflows_total = int(
        db.scalar(select(func.count()).select_from(Workflow).where(Workflow.owner_id == owner_id, Workflow.archived.is_(False))) or 0
    )
    latest_event_at = db.scalar(
        select(func.max(RunEvent.created_at)).where(
            RunEvent.run_id.in_(select(WorkflowRun.id).where(WorkflowRun.workflow_id.in_(owned_workflows)))
        )
    )

    stats = {
        "window_hours": window_hours,
        "runs_total": total_runs,
        "runs_by_status": [{"status": status, "count": count} for status, count in sorted(status_counts.items())],
        "runs_started": started,
        "runs_finished": len(finished_rows),
        "success_rate": success_rate,
        "avg_duration_seconds": round(sum(durations) / len(durations), 3) if durations else None,
        "p95_duration_seconds": _percentile(durations, 0.95),
        "step_attempts": attempts,
        "retried_steps": retried,
        "failed_steps": step_counts.get("failed", 0),
        "timed_out_steps": timed_out,
        "active_workers": active_workers,
        "total_workers": len(workers),
        "pending_steps": pending_steps,
        "running_steps": running_steps,
        "schedules_enabled": schedules_enabled,
        "schedules_due": schedules_due,
        "workflows_total": workflows_total,
        "latest_event_at": latest_event_at,
    }

    recent_runs = list(
        db.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id.in_(owned_workflows))
            .order_by(WorkflowRun.created_at.desc())
            .limit(recent_limit)
        ).all()
    )
    recent_workflow_ids = {run.workflow_id for run in recent_runs}
    workflow_names = {
        row.id: row.name
        for row in db.scalars(select(Workflow).where(Workflow.id.in_(recent_workflow_ids or {""}))).all()
    }
    from app.services import run_service

    recent_run_ids = [run.id for run in recent_runs]
    step_rows = list(db.scalars(select(StepRun).where(StepRun.run_id.in_(recent_run_ids or [""]))).all()) if recent_run_ids else []
    steps_by_run: dict[str, list[StepRun]] = {}
    for step in step_rows:
        steps_by_run.setdefault(step.run_id, []).append(step)

    recent_summaries = [
        run_service.run_summary_view(db, run, workflow_name=workflow_names.get(run.workflow_id, ""), steps=steps_by_run.get(run.id, []))
        for run in recent_runs
    ]

    activity = _activity(db, owner_id, workflow_names=workflow_names, limit=12)

    worker_load = []
    running_by_worker = dict(
        db.execute(
            select(StepRun.worker_id, func.count())
            .where(StepRun.status == "running", StepRun.worker_id.is_not(None))
            .group_by(StepRun.worker_id)
        ).all()
    )
    for worker in workers[:12]:
        last_seen = _as_utc(worker.last_seen_at)
        worker_load.append(
            {
                "worker_id": worker.id,
                "name": worker.name,
                "active": worker.active,
                "stale": worker.is_stale,
                "task_types": list(worker.task_types or []),
                "max_concurrency": worker.max_concurrency,
                "running_steps": int(running_by_worker.get(worker.id, 0)),
                "last_seen_at": worker.last_seen_at,
                "last_seen_seconds_ago": round(max(0.0, (now - last_seen).total_seconds()), 1),
            }
        )

    top_workflows = [
        {"workflow_id": workflow_id, "name": name, "runs": count}
        for workflow_id, name, count in db.execute(
            select(WorkflowRun.workflow_id, Workflow.name, func.count())
            .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
            .where(Workflow.owner_id == owner_id, WorkflowRun.created_at >= since)
            .group_by(WorkflowRun.workflow_id, Workflow.name)
            .order_by(func.count().desc())
            .limit(5)
        ).all()
    ]

    timeline, duration_trend = _charts(db, owned_workflows, since=since, window_hours=window_hours)
    attention = _needs_attention(db, owner_id, owned_workflows, workflow_names=_all_workflow_names(db, owner_id), now=now.replace(tzinfo=None))

    return {
        "stats": stats,
        "recent_runs": recent_summaries,
        "recent_activity": activity,
        "workers": worker_load,
        "top_workflows": top_workflows,
        "needs_attention": attention,
        "runs_timeline": timeline,
        "duration_trend": duration_trend,
    }


def _all_workflow_names(db: Session, owner_id: str) -> dict[str, str]:
    return {
        row.id: row.name for row in db.scalars(select(Workflow).where(Workflow.owner_id == owner_id)).all()
    }


def _charts(
    db: Session,
    owned_workflows: Any,
    *,
    since: datetime,
    window_hours: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Runs-over-time counts and average durations bucketed across the window.

    At most 24 buckets; every bucket is emitted so the chart shows empty spans
    instead of collapsing the time axis.
    """
    bucket_count = min(24, max(1, window_hours))
    bucket_seconds = max(1, int(window_hours * 3600 / bucket_count))
    start = ensure_utc(since).replace(tzinfo=None)
    buckets: list[dict[str, Any]] = []
    for index in range(bucket_count):
        bucket_start = start + timedelta(seconds=bucket_seconds * index)
        buckets.append(
            {
                "bucket_start": bucket_start,
                "label": bucket_start.strftime("%H:%M") if window_hours <= 48 else bucket_start.strftime("%m-%d"),
                "started": 0,
                "succeeded": 0,
                "failed": 0,
                "durations": [],
            }
        )

    def _bucket_index(value: datetime | None) -> int | None:
        if value is None or value < start:
            return None
        return min(int((value - start).total_seconds() // bucket_seconds), bucket_count - 1)

    rows = db.execute(
        select(WorkflowRun.created_at, WorkflowRun.started_at, WorkflowRun.finished_at, WorkflowRun.status)
        .where(WorkflowRun.workflow_id.in_(owned_workflows), WorkflowRun.created_at >= since)
    ).all()
    for created_at, started_at, finished_at, status in rows:
        index = _bucket_index(ensure_utc(created_at).replace(tzinfo=None))
        if index is None:
            continue
        bucket = buckets[index]
        bucket["started"] += 1
        if status == "succeeded":
            bucket["succeeded"] += 1
        elif status == "failed":
            bucket["failed"] += 1
        if finished_at is not None and started_at is not None:
            bucket["durations"].append((ensure_utc(finished_at) - ensure_utc(started_at)).total_seconds())

    timeline = [
        {
            "bucket_start": bucket["bucket_start"],
            "label": bucket["label"],
            "started": bucket["started"],
            "succeeded": bucket["succeeded"],
            "failed": bucket["failed"],
        }
        for bucket in buckets
    ]
    duration_trend = [
        {
            "bucket_start": bucket["bucket_start"],
            "label": bucket["label"],
            "avg_duration_seconds": round(sum(bucket["durations"]) / len(bucket["durations"]), 3)
            if bucket["durations"]
            else None,
        }
        for bucket in buckets
    ]
    return timeline, duration_trend


def _needs_attention(
    db: Session,
    owner_id: str,
    owned_workflows: Any,
    *,
    workflow_names: dict[str, str],
    now: datetime,
) -> list[dict[str, Any]]:
    """Operator queue: everything that wants a human decision, newest first."""
    items: list[dict[str, Any]] = []

    waiting_rows = db.execute(
        select(StepRun, WorkflowRun)
        .join(WorkflowRun, WorkflowRun.id == StepRun.run_id)
        .where(WorkflowRun.workflow_id.in_(owned_workflows), StepRun.status == "waiting_approval")
        .order_by(StepRun.created_at.asc())
        .limit(10)
    ).all()
    for step, run in waiting_rows:
        items.append(
            {
                "kind": "approval",
                "id": step.id,
                "run_id": run.id,
                "workflow_id": run.workflow_id,
                "step_key": step.step_key,
                "severity": "warning",
                "label": "Approval needed",
                "detail": f"{workflow_names.get(run.workflow_id, run.workflow_id)} · step {step.step_key}",
                "at": step.created_at,
            }
        )

    failed_runs = list(
        db.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id.in_(owned_workflows), WorkflowRun.status == "failed")
            .order_by(WorkflowRun.created_at.desc())
            .limit(5)
        ).all()
    )
    for run in failed_runs:
        items.append(
            {
                "kind": "run_failed",
                "id": run.id,
                "run_id": run.id,
                "workflow_id": run.workflow_id,
                "severity": "bad",
                "label": "Run failed",
                "detail": f"{workflow_names.get(run.workflow_id, run.workflow_id)} · v{run.version} · {run.trigger}",
                "at": run.created_at,
            }
        )

    paused_runs = list(
        db.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id.in_(owned_workflows), WorkflowRun.status == "paused")
            .order_by(WorkflowRun.created_at.desc())
            .limit(5)
        ).all()
    )
    for run in paused_runs:
        items.append(
            {
                "kind": "run_paused",
                "id": run.id,
                "run_id": run.id,
                "workflow_id": run.workflow_id,
                "severity": "warning",
                "label": "Run paused",
                "detail": f"{workflow_names.get(run.workflow_id, run.workflow_id)} · v{run.version}",
                "at": run.created_at,
            }
        )

    stale_cutoff = now - timedelta(seconds=settings.worker_stale_seconds)
    for worker in db.scalars(select(Worker).where(Worker.active.is_(True))).all():
        last_seen = ensure_utc(worker.last_seen_at)
        if last_seen is not None and last_seen.replace(tzinfo=None) < stale_cutoff:
            items.append(
                {
                    "kind": "worker_stale",
                    "id": worker.id,
                    "run_id": None,
                    "workflow_id": None,
                    "severity": "warning",
                    "label": "Worker stale",
                    "detail": f"{worker.name or worker.id} last seen {last_seen.replace(tzinfo=None).isoformat()}Z",
                    "at": worker.last_seen_at,
                }
            )

    dlq_count = int(
        db.scalar(
            select(func.count())
            .select_from(StepRun)
            .join(WorkflowRun, WorkflowRun.id == StepRun.run_id)
            .where(WorkflowRun.workflow_id.in_(owned_workflows), StepRun.status == "failed")
        )
        or 0
    )
    if dlq_count:
        items.append(
            {
                "kind": "dlq",
                "id": "dlq",
                "run_id": None,
                "workflow_id": None,
                "severity": "bad",
                "label": "Dead letters",
                "detail": f"{dlq_count} failed step{'s' if dlq_count != 1 else ''} waiting for redrive",
                "at": now,
            }
        )

    due_schedules = int(
        db.scalar(
            select(func.count())
            .select_from(WorkflowSchedule)
            .where(
                WorkflowSchedule.owner_id == owner_id,
                WorkflowSchedule.enabled.is_(True),
                WorkflowSchedule.next_run_at.is_not(None),
                WorkflowSchedule.next_run_at <= now,
            )
        )
        or 0
    )
    if due_schedules:
        items.append(
            {
                "kind": "schedule_due",
                "id": "schedules-due",
                "run_id": None,
                "workflow_id": None,
                "severity": "warning",
                "label": "Schedules due",
                "detail": f"{due_schedules} schedule{'s' if due_schedules != 1 else ''} waiting to fire",
                "at": now,
            }
        )

    paused_schedules = int(
        db.scalar(
            select(func.count()).select_from(WorkflowSchedule).where(
                WorkflowSchedule.owner_id == owner_id, WorkflowSchedule.enabled.is_(False)
            )
        )
        or 0
    )
    if paused_schedules:
        items.append(
            {
                "kind": "schedule_paused",
                "id": "schedules-paused",
                "run_id": None,
                "workflow_id": None,
                "severity": "neutral",
                "label": "Schedules paused",
                "detail": f"{paused_schedules} schedule{'s' if paused_schedules != 1 else ''} disabled",
                "at": now,
            }
        )

    def _sort_key(item: dict[str, Any]) -> datetime:
        value = item.get("at")
        if isinstance(value, datetime):
            return ensure_utc(value).replace(tzinfo=None)
        return now

    items.sort(key=_sort_key, reverse=True)
    for item in items:
        value = item.get("at")
        if isinstance(value, datetime):
            item["at"] = ensure_utc(value).isoformat().replace("+00:00", "Z")
    return items


def _activity(db: Session, owner_id: str, *, workflow_names: dict[str, str], limit: int = 12) -> list[dict[str, Any]]:
    """Merge workflow-level events and recent run outcomes into one feed."""
    events = list(
        db.scalars(
            select(RunEvent)
            .where(
                RunEvent.workflow_id.in_(select(Workflow.id).where(Workflow.owner_id == owner_id)),
                RunEvent.event_type.in_(
                    [
                        EventType.WORKFLOW_PUBLISHED,
                        EventType.WORKFLOW_CREATED,
                        EventType.WORKFLOW_ARCHIVED,
                        EventType.SCHEDULE_CREATED,
                        EventType.SCHEDULE_TRIGGERED,
                        EventType.SCHEDULE_SKIPPED,
                        EventType.SCHEDULE_FAILED,
                    ]
                ),
            )
            .order_by(RunEvent.seq.desc())
            .limit(limit)
        ).all()
    )
    items: list[dict[str, Any]] = []
    for event in events:
        items.append(
            {
                "kind": "event",
                "id": event.id,
                "run_id": event.run_id,
                "workflow_id": event.workflow_id,
                "workflow_name": workflow_names.get(event.workflow_id or "", ""),
                "status": event.event_type,
                "label": _event_label(event.event_type),
                "detail": event.message,
                "at": event.created_at,
            }
        )
    runs = list(
        db.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id.in_(select(Workflow.id).where(Workflow.owner_id == owner_id)))
            .order_by(WorkflowRun.created_at.desc())
            .limit(limit)
        ).all()
    )
    for run in runs:
        items.append(
            {
                "kind": "run",
                "id": run.id,
                "run_id": run.id,
                "workflow_id": run.workflow_id,
                "workflow_name": workflow_names.get(run.workflow_id, ""),
                "status": run.status,
                "label": f"Run {run.status}",
                "detail": f"version {run.version} · {run.trigger}",
                "at": run.finished_at or run.started_at or run.created_at,
            }
        )
    items.sort(key=lambda item: item["at"], reverse=True)
    return items[:limit]


def _event_label(event_type: str) -> str:
    return {
        EventType.WORKFLOW_PUBLISHED: "Workflow published",
        EventType.WORKFLOW_CREATED: "Workflow created",
        EventType.WORKFLOW_ARCHIVED: "Workflow archived",
        EventType.SCHEDULE_CREATED: "Schedule created",
        EventType.SCHEDULE_TRIGGERED: "Schedule fired",
        EventType.SCHEDULE_SKIPPED: "Schedule skipped",
        EventType.SCHEDULE_FAILED: "Schedule failed",
    }.get(event_type, event_type)


__all__ = ["build_dashboard"]
