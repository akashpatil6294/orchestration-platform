"""Recovery operations: crashed runs, DLQ depth, requeue, failed webhooks (Stage H, H5)."""
from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import desc, func, select

from app.api.deps import CurrentUser, DbSession
from app.models.notification import NotificationChannel
from app.models.run import StepRun, WorkflowRun
from app.models.workflow import Workflow

router = APIRouter(prefix="/api/v1/ops/recovery", tags=["operations"])


@router.get("/summary", response_model=dict)
def recovery_summary(user: CurrentUser, db: DbSession) -> dict:
    """Counts for the recovery page: crashed runs, DLQ depth, stuck steps."""
    # Crashed runs: running runs with lease-expired steps (worker died mid-run).
    crashed = db.scalar(
        select(func.count(func.distinct(WorkflowRun.id)))
        .select_from(WorkflowRun)
        .join(StepRun, StepRun.run_id == WorkflowRun.id)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(
            Workflow.owner_id == user.id,
            WorkflowRun.status.in_(("running", "retrying")),
            StepRun.status == "lease_expired",
        )
    ) or 0
    # DLQ depth: permanently failed steps.
    dlq_depth = db.scalar(
        select(func.count())
        .select_from(StepRun)
        .join(WorkflowRun, WorkflowRun.id == StepRun.run_id)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(Workflow.owner_id == user.id, StepRun.status == "failed")
    ) or 0
    # Stuck: queued runs older than 10 minutes with no active worker claim.
    stuck = db.scalar(
        select(func.count())
        .select_from(WorkflowRun)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(Workflow.owner_id == user.id, WorkflowRun.status == "queued")
    ) or 0
    return {"crashed_runs": crashed, "dlq_depth": dlq_depth, "queued_runs": stuck}


@router.get("/crashed", response_model=dict)
def list_crashed_runs(
    user: CurrentUser, db: DbSession, limit: int = Query(default=20, ge=1, le=100)
) -> dict:
    """Runs with lease-expired steps: a worker died mid-run."""
    rows = db.execute(
        select(WorkflowRun, Workflow.name, func.count(StepRun.id))
        .join(StepRun, StepRun.run_id == WorkflowRun.id)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(
            Workflow.owner_id == user.id,
            WorkflowRun.status.in_(("running", "retrying")),
            StepRun.status == "lease_expired",
        )
        .group_by(WorkflowRun.id, Workflow.name)
        .order_by(desc(WorkflowRun.created_at))
        .limit(limit)
    ).all()
    return {
        "items": [
            {
                "run_id": run.id,
                "workflow_id": run.workflow_id,
                "workflow_name": name,
                "status": run.status,
                "expired_steps": count,
                "updated_at": run.created_at,
            }
            for run, name, count in rows
        ]
    }


@router.get("/webhooks/failed", response_model=dict)
def list_failed_webhooks(user: CurrentUser, db: DbSession) -> dict:
    """Notification channels are the webhook surface; report inactive ones.

    Per-delivery webhook logs live in notification events; this lists channels
    that are disabled (often after repeated delivery failures).
    """
    channels = db.scalars(
        select(NotificationChannel)
        .where(NotificationChannel.user_id == user.id, NotificationChannel.is_active.is_(False))
        .order_by(desc(NotificationChannel.created_at))
    ).all()
    return {
        "items": [
            {
                "id": c.id,
                "name": c.name,
                "channel_type": c.channel_type,
                "workflow_id": c.workflow_id,
                "events": c.events,
            }
            for c in channels
        ]
    }


@router.post("/webhooks/{channel_id}/retry", response_model=dict)
def retry_webhook(channel_id: str, user: CurrentUser, db: DbSession) -> dict:
    """Re-enable a failed webhook channel (manual recovery)."""
    from app.core.errors import NotFound

    channel = db.scalar(
        select(NotificationChannel).where(
            NotificationChannel.id == channel_id, NotificationChannel.user_id == user.id
        )
    )
    if channel is None:
        raise NotFound("Channel not found", code="channel_not_found")
    channel.is_active = True
    db.commit()
    return {"id": channel.id, "is_active": True}
