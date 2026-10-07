"""Refresh Prometheus gauges that are derived from database state.

Called on every ``/metrics`` scrape so the gauges reflect the current
database without a background loop: queue depth per queue, dead-letter
queue size, and inflight run/step counts.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.metrics import gauge
from app.models.run import StepRun, WorkflowRun


def refresh_operational_gauges(db: Session) -> None:
    queue_depths = {
        queue or "default": int(count)
        for queue, count in db.execute(
            select(StepRun.queue_name, func.count())
            .where(StepRun.status.in_(("pending", "retrying")))
            .group_by(StepRun.queue_name)
        ).all()
    }
    if "default" not in queue_depths:
        queue_depths["default"] = 0
    for queue, depth in queue_depths.items():
        gauge("orchestrator_queue_depth", float(depth), {"queue": queue})

    dlq_size = int(
        db.scalar(select(func.count()).select_from(StepRun).where(StepRun.status == "failed")) or 0
    )
    gauge("orchestrator_dlq_size", float(dlq_size))

    runs_inflight = int(
        db.scalar(
            select(func.count())
            .select_from(WorkflowRun)
            .where(WorkflowRun.status.in_(("queued", "running", "cancelling")))
        )
        or 0
    )
    gauge("orchestrator_runs_inflight", float(runs_inflight))

    steps_inflight = int(
        db.scalar(
            select(func.count())
            .select_from(StepRun)
            .where(StepRun.status.in_(("pending", "running", "retrying")))
        )
        or 0
    )
    gauge("orchestrator_steps_inflight", float(steps_inflight))


__all__ = ["refresh_operational_gauges"]
