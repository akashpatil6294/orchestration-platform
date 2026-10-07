"""Schedule routes: create, edit, pause, resume, preview and fire.

Creation is nested under the workflow (``POST /workflows/{id}/schedules``) so a
schedule can never be created without an owned workflow, while listing and
mutation live at the top level because a schedule id is globally unique.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.workflow import Workflow
from app.schemas.schedule import (
    CronPreviewRequest,
    CronPreviewResponse,
    ScheduleBackfillRequest,
    ScheduleBackfillView,
    ScheduleCreate,
    ScheduleFireResponse,
    ScheduleToggleRequest,
    ScheduleUpdate,
    ScheduleView,
)
from app.services import scheduler_service, workflow_service

router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"])
workflow_router = APIRouter(prefix="/api/v1/workflows/{workflow_id}/schedules", tags=["schedules"])


def _view(db, schedule, names: dict[str, str] | None = None) -> dict:
    name = (names or {}).get(schedule.workflow_id)
    if name is None:
        workflow = db.get(Workflow, schedule.workflow_id)
        name = workflow.name if workflow else ""
    return scheduler_service.schedule_view(db, schedule, workflow_name=name)


def _backfill_view(job) -> dict:
    return {
        "id": job.id,
        "schedule_id": job.schedule_id,
        "start": job.start_at,
        "end": job.end_at,
        "next_slot_at": job.next_slot_at,
        "concurrency_limit": job.concurrency_limit,
        "status": job.status,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
    }


def _names_for(db, workflow_ids: set[str]) -> dict[str, str]:
    return {
        workflow.id: workflow.name
        for workflow in db.scalars(select(Workflow).where(Workflow.id.in_(workflow_ids or {""}))).all()
    }


@router.post("/preview", response_model=CronPreviewResponse)
def preview_cron(payload: CronPreviewRequest, user: CurrentUser) -> dict:
    """Validate a cron expression and show its next fire times."""
    return scheduler_service.preview(payload.cron_expression, payload.timezone, payload.count)


@router.get("", response_model=dict)
def list_schedules(
    user: CurrentUser,
    db: DbSession,
    workflow_id: str | None = Query(default=None),
    enabled: bool | None = Query(default=None),
) -> dict:
    schedules = scheduler_service.list_schedules(db, user.id, workflow_id=workflow_id, enabled=enabled)
    names = _names_for(db, {schedule.workflow_id for schedule in schedules})
    items = [_view(db, schedule, names) for schedule in schedules]
    return {"items": items, "total": len(items), "stats": scheduler_service.schedule_stats(db, user.id)}


@router.get("/{schedule_id}", response_model=ScheduleView)
def get_schedule(schedule_id: str, user: CurrentUser, db: DbSession) -> dict:
    schedule = scheduler_service.get_schedule(db, schedule_id, user.id)
    return _view(db, schedule)


@router.patch("/{schedule_id}", response_model=ScheduleView)
def update_schedule(schedule_id: str, payload: ScheduleUpdate, user: CurrentUser, db: DbSession) -> dict:
    schedule = scheduler_service.get_schedule(db, schedule_id, user.id)
    scheduler_service.update_schedule(db, schedule, payload)
    db.commit()
    db.refresh(schedule)
    return _view(db, schedule)


@router.post("/{schedule_id}/toggle", response_model=ScheduleView)
def toggle_schedule(schedule_id: str, payload: ScheduleToggleRequest, user: CurrentUser, db: DbSession) -> dict:
    schedule = scheduler_service.get_schedule(db, schedule_id, user.id)
    scheduler_service.update_schedule(db, schedule, ScheduleUpdate(enabled=payload.enabled))
    db.commit()
    db.refresh(schedule)
    return _view(db, schedule)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schedule(schedule_id: str, user: CurrentUser, db: DbSession) -> None:
    schedule = scheduler_service.get_schedule(db, schedule_id, user.id)
    scheduler_service.delete_schedule(db, schedule)
    db.commit()


@router.post("/{schedule_id}/run-now", response_model=ScheduleFireResponse)
def run_now(schedule_id: str, user: CurrentUser, db: DbSession) -> dict:
    """Fire a schedule immediately, outside its normal cadence."""
    schedule = scheduler_service.get_schedule(db, schedule_id, user.id)
    result = scheduler_service.fire_schedule(db, schedule, actor=user.email)
    db.commit()
    return {
        "schedule_id": schedule.id,
        "run_id": result.get("run_id"),
        "status": result["status"],
        "message": result.get("message", ""),
    }


@router.post("/{schedule_id}/backfill", response_model=ScheduleBackfillView, status_code=status.HTTP_202_ACCEPTED)
def create_backfill(
    schedule_id: str,
    payload: ScheduleBackfillRequest,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    schedule = scheduler_service.get_schedule(db, schedule_id, user.id)
    job = scheduler_service.create_backfill(
        db,
        schedule,
        user.id,
        start=payload.start,
        end=payload.end,
        concurrency_limit=payload.concurrency_limit,
    )
    db.commit()
    db.refresh(job)
    return _backfill_view(job)


@router.get("/{schedule_id}/backfills", response_model=dict)
def list_backfills(schedule_id: str, user: CurrentUser, db: DbSession) -> dict:
    from app.models.schedule import ScheduleBackfill

    schedule = scheduler_service.get_schedule(db, schedule_id, user.id)
    jobs = db.scalars(
        select(ScheduleBackfill)
        .where(ScheduleBackfill.schedule_id == schedule.id, ScheduleBackfill.owner_id == user.id)
        .order_by(ScheduleBackfill.created_at.desc())
    ).all()
    return {"items": [_backfill_view(job) for job in jobs], "total": len(jobs)}


@workflow_router.post("", response_model=ScheduleView, status_code=status.HTTP_201_CREATED)
def create_schedule(workflow_id: str, payload: ScheduleCreate, user: CurrentUser, db: DbSession) -> dict:
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    schedule = scheduler_service.create_schedule(db, user.id, workflow, payload)
    db.commit()
    db.refresh(schedule)
    return _view(db, schedule, {workflow.id: workflow.name})


@workflow_router.get("", response_model=dict)
def list_workflow_schedules(workflow_id: str, user: CurrentUser, db: DbSession) -> dict:
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    schedules = scheduler_service.list_schedules(db, user.id, workflow_id=workflow.id)
    items = [_view(db, schedule, {workflow.id: workflow.name}) for schedule in schedules]
    return {"items": items, "total": len(items), "stats": scheduler_service.schedule_stats(db, user.id)}


__all__ = ["router", "workflow_router"]
