"""Owner-scoped dead-letter visibility and redrive controls."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.errors import Conflict, NotFound
from app.models.run import StepRun, WorkflowRun
from app.models.workflow import Workflow
from app.services import run_service

router = APIRouter(prefix="/api/v1/dlq", tags=["operations"])


@router.get("")
def list_dead_letters(
    user: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict:
    rows = db.execute(
        select(StepRun, WorkflowRun, Workflow.name)
        .join(WorkflowRun, WorkflowRun.id == StepRun.run_id)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(Workflow.owner_id == user.id, StepRun.status == "failed")
        .order_by(StepRun.finished_at.desc(), StepRun.created_at.desc())
        .limit(limit)
    ).all()
    return {
        "items": [
            {
                "step_run_id": step.id,
                "run_id": run.id,
                "workflow_id": run.workflow_id,
                "workflow_name": name,
                "step_key": step.step_key,
                "task_type": step.task_type,
                "attempts": step.attempts,
                "retry_limit": step.retry_limit,
                "lease_expirations": step.lease_expirations,
                "priority": step.priority,
                "queue": step.queue_name,
                "error": step.error,
                "failed_at": step.finished_at,
            }
            for step, run, name in rows
        ]
    }


@router.post("/{step_run_id}/redrive")
def redrive_dead_letter(step_run_id: str, user: CurrentUser, db: DbSession) -> dict:
    """Schedule a failed step for another attempt.

    Idempotent: while the step is already scheduled or running, a repeated
    redrive request reports ``already_redriven`` instead of scheduling the work
    a second time.
    """
    step = db.get(StepRun, step_run_id)
    if step is None:
        raise NotFound("Dead-letter task not found", code="dead_letter_not_found")
    run = run_service.get_run(db, step.run_id, user.id)
    if step.status != "failed":
        if step.status in {"pending", "retrying", "running"}:
            return {
                "run_id": run.id,
                "step_run_id": step.id,
                "status": run.status,
                "already_redriven": True,
                "retried_steps": [],
                "reset_steps": [],
            }
        raise Conflict("Only failed tasks can be redriven", code="dead_letter_not_failed")
    result = run_service.retry_failed_steps(
        db,
        run,
        step_keys=[step.step_key],
        reset_downstream=True,
        actor=user.email,
    )
    db.commit()
    db.refresh(run)
    return {"run_id": run.id, "step_run_id": step.id, "status": run.status, "already_redriven": False, **result}


__all__ = ["router"]
