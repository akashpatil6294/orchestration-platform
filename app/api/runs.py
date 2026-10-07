"""Run routes: history, detail, live events, attempts, cancellation and retry."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.config import settings
from app.core.pagination import Pagination, page_of, pagination
from app.models.run import StepAttempt, StepRun, WorkflowRun
from app.models.workflow import Workflow
from app.schemas.run import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    CancelResponse,
    DashboardResponse,
    ReplayRunRequest,
    RetryRunRequest,
    RetryRunResponse,
    RunControlResponse,
    RunDetail,
    RunEventsResponse,
    RunSummary,
    StepAttemptsResponse,
)
from app.services import artifact_service, dashboard_service, event_service, run_service

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


@router.get("", response_model=dict)
def list_runs(
    user: CurrentUser,
    db: DbSession,
    page: Annotated[Pagination, Depends(pagination)],
    workflow_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status", max_length=24),
    trigger: str | None = Query(default=None, max_length=24),
    search: str | None = Query(default=None, max_length=200),
    window_hours: int | None = Query(default=None, ge=1, le=24 * 90),
    created_before: datetime | None = Query(default=None),
    sort: str | None = Query(default=None, pattern="^(newest|oldest|longest|status)$"),
) -> dict:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    since = now - timedelta(hours=window_hours) if window_hours else None
    rows, total = run_service.list_runs(
        db, user.id, workflow_id=workflow_id, status=status_filter, trigger=trigger, search=search,
        since=since, until=created_before, sort=sort or "newest", limit=page.limit, offset=page.offset,
    )
    ids = [row.id for row in rows]
    names = {
        workflow.id: workflow.name
        for workflow in db.scalars(select(Workflow).where(Workflow.id.in_({row.workflow_id for row in rows} or {""}))).all()
    }
    steps_by_run: dict[str, list[StepRun]] = {}
    if ids:
        for step in db.scalars(select(StepRun).where(StepRun.run_id.in_(ids))).all():
            steps_by_run.setdefault(step.run_id, []).append(step)
    items = [
        run_service.run_summary_view(db, row, workflow_name=names.get(row.workflow_id, ""), steps=steps_by_run.get(row.id, []))
        for row in rows
    ]
    return page_of(items, total, page)


@router.get("/dashboard", response_model=DashboardResponse)
def dashboard(
    user: CurrentUser,
    db: DbSession,
    window_hours: int = Query(default=settings.default_dashboard_window_hours, ge=1, le=24 * 30),
    recent: int = Query(default=8, ge=1, le=25),
) -> dict:
    return dashboard_service.build_dashboard(db, user.id, window_hours=window_hours, recent_limit=recent)


@router.get("/{run_id}", response_model=RunDetail)
def get_run(run_id: str, user: CurrentUser, db: DbSession) -> dict:
    run = run_service.get_run(db, run_id, user.id)
    workflow = db.get(Workflow, run.workflow_id)
    return run_service.run_detail_view(db, run, workflow_name=workflow.name if workflow else "")


@router.get("/{run_id}/artifacts/{artifact_id}")
def get_run_artifact(run_id: str, artifact_id: str, user: CurrentUser, db: DbSession) -> Response:
    run = run_service.get_run(db, run_id, user.id)
    artifact = artifact_service.get_run_artifact(db, run.id, artifact_id)
    data = artifact_service.read_artifact(artifact)
    return Response(
        content=data,
        media_type=artifact.content_type,
        headers={"Content-Disposition": f'attachment; filename="{artifact.id}.json"'},
    )


@router.get("/{run_id}/events", response_model=RunEventsResponse)
def get_run_events(
    run_id: str,
    user: CurrentUser,
    db: DbSession,
    after_seq: int = Query(default=0, ge=0, description="Return events newer than this sequence number"),
    limit: int = Query(default=200, ge=1, le=1000),
    level: str | None = Query(default=None, pattern="^(debug|info|warning|error)$"),
    step_key: str | None = Query(default=None, max_length=128),
) -> dict:
    run = run_service.get_run(db, run_id, user.id)
    events = event_service.list_events(db, run.id, after_seq=after_seq, limit=limit, level=level, step_key=step_key)
    latest = event_service.latest_seq(db, run.id)
    return {
        "items": [
            {
                "seq": event.seq,
                "id": event.id,
                "type": event.event_type,
                "level": event.level,
                "message": event.message,
                "step_run_id": event.step_run_id,
                "step_key": event.step_key,
                "actor": event.actor,
                "payload": event.payload,
                "created_at": event.created_at,
            }
            for event in events
        ],
        "latest_seq": latest,
        "has_more": bool(events) and events[-1].seq < latest,
    }


@router.get("/{run_id}/events/stream")
def stream_run_events(
    run_id: str,
    request: Request,
    user: CurrentUser,
    db: DbSession,
    after_seq: int = Query(default=0, ge=0),
) -> StreamingResponse:
    """Server-sent events for a run: replays events after ``after_seq``, then
    streams new ones until the run settles or 5 minutes pass."""
    from fastapi.responses import StreamingResponse

    run = run_service.get_run(db, run_id, user.id)
    run_id_value = run.id

    def _event_dict(event) -> dict:
        return {
            "seq": event.seq,
            "id": event.id,
            "type": event.event_type,
            "level": event.level,
            "message": event.message,
            "step_run_id": event.step_run_id,
            "step_key": event.step_key,
            "created_at": event.created_at.isoformat() if event.created_at else None,
        }

    def generate():
        from app.database import SessionLocal

        seq = after_seq
        # Replay.
        existing = event_service.list_events(db, run_id_value, after_seq=seq, limit=1000)
        for event in existing:
            seq = max(seq, event.seq)
            yield f"data: {json.dumps(_event_dict(event))}\n\n"
        # Stream until terminal or timeout. Uses a fresh session per poll
        # because the request session is bound to this thread's iteration.
        deadline = time.monotonic() + 300
        session = SessionLocal()
        try:
            while time.monotonic() < deadline:
                if request.is_disconnected():
                    break
                fresh = session.get(WorkflowRun, run_id_value)
                events = event_service.list_events(session, run_id_value, after_seq=seq, limit=200)
                for event in events:
                    seq = max(seq, event.seq)
                    yield f"data: {json.dumps(_event_dict(event))}\n\n"
                if fresh is not None and fresh.status in run_service.TERMINAL_RUN_STATUSES:
                    break
                session.expire_all()
                time.sleep(1.0)
        finally:
            session.close()
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.get("/{run_id}/steps/{step_run_id}/attempts", response_model=StepAttemptsResponse)
def get_attempts(run_id: str, step_run_id: str, user: CurrentUser, db: DbSession) -> dict:
    run = run_service.get_run(db, run_id, user.id)
    step = db.scalar(select(StepRun).where(StepRun.id == step_run_id, StepRun.run_id == run.id))
    if step is None:
        from app.core.errors import NotFound

        raise NotFound("Step not found in this run", code="step_not_found")
    attempts = db.scalars(
        select(StepAttempt).where(StepAttempt.step_run_id == step.id).order_by(StepAttempt.attempt_no)
    ).all()
    return {"step_run_id": step.id, "step_key": step.step_key, "items": [run_service.attempt_view(row) for row in attempts]}


@router.get("/{run_id}/steps/{step_run_id}/logs")
def get_step_logs(
    run_id: str,
    step_run_id: str,
    user: CurrentUser,
    db: DbSession,
    attempt: int | None = Query(default=None, ge=1),
) -> dict:
    """Logs for one step, optionally for a specific attempt."""
    run = run_service.get_run(db, run_id, user.id)
    step = db.scalar(select(StepRun).where(StepRun.id == step_run_id, StepRun.run_id == run.id))
    if step is None:
        from app.core.errors import NotFound

        raise NotFound("Step not found in this run", code="step_not_found")
    if attempt is None:
        return {"step_run_id": step.id, "step_key": step.step_key, "attempt": None, "lines": list(step.logs or [])}
    row = db.scalar(select(StepAttempt).where(StepAttempt.step_run_id == step.id, StepAttempt.attempt_no == attempt))
    if row is None:
        from app.core.errors import NotFound

        raise NotFound("That attempt does not exist", code="attempt_not_found")
    return {"step_run_id": step.id, "step_key": step.step_key, "attempt": row.attempt_no, "lines": list(row.logs or [])}


@router.post("/{run_id}/cancel", response_model=CancelResponse)
def cancel_run(run_id: str, user: CurrentUser, db: DbSession) -> dict:
    run = run_service.get_run(db, run_id, user.id)
    if run.status in run_service.TERMINAL_RUN_STATUSES:
        return {"id": run.id, "status": run.status, "cancel_requested": run.cancel_requested, "message": "This run has already finished"}
    run_service.request_cancel(db, run, actor=user.email)
    db.commit()
    db.refresh(run)
    message = (
        "Cancellation requested. Running steps are being asked to stop."
        if run.status == "cancelling"
        else "Run cancelled."
    )
    return {"id": run.id, "status": run.status, "cancel_requested": run.cancel_requested, "message": message}


@router.post("/{run_id}/pause", response_model=RunControlResponse)
def pause_run(run_id: str, user: CurrentUser, db: DbSession) -> dict:
    run = run_service.get_run(db, run_id, user.id)
    run_service.pause_run(db, run, actor=user.email)
    db.commit()
    return {"id": run.id, "status": run.status, "message": "Run paused"}


@router.post("/{run_id}/resume", response_model=RunControlResponse)
def resume_run(run_id: str, user: CurrentUser, db: DbSession) -> dict:
    run = run_service.get_run(db, run_id, user.id)
    run_service.resume_run(db, run, actor=user.email)
    db.commit()
    return {"id": run.id, "status": run.status, "message": "Run resumed"}


@router.post("/{run_id}/steps/{step_key}/approval", response_model=ApprovalDecisionResponse)
def decide_approval(run_id: str, step_key: str, payload: ApprovalDecisionRequest, user: CurrentUser, db: DbSession) -> dict:
    run = run_service.get_run(db, run_id, user.id)
    step = run_service.decide_approval(
        db,
        run,
        step_key=step_key,
        actor=user.email,
        decision=payload.decision,
        comment=payload.comment,
        is_admin=bool(user.is_admin),
    )
    db.commit()
    return {"run_id": run.id, "step_key": step.step_key, "status": step.status, "actor": user.email, "comment": payload.comment}


@router.post("/{run_id}/retry", response_model=RetryRunResponse)
def retry_run(run_id: str, payload: RetryRunRequest, user: CurrentUser, db: DbSession) -> dict:
    run = run_service.get_run(db, run_id, user.id)
    result = run_service.retry_failed_steps(
        db, run, step_keys=payload.steps or None, reset_downstream=payload.reset_downstream,
        input_data=payload.input, actor=user.email,
    )
    db.commit()
    db.refresh(run)
    return {"id": run.id, "status": run.status, **result}


@router.post("/{run_id}/rerun-from-step", response_model=RetryRunResponse)
def rerun_from_step(run_id: str, payload: RetryRunRequest, user: CurrentUser, db: DbSession) -> dict:
    run = run_service.get_run(db, run_id, user.id)
    if len(payload.steps) != 1:
        from app.core.errors import Invalid

        raise Invalid("Provide exactly one step key to rerun from", code="step_key_required")
    result = run_service.retry_failed_steps(
        db, run, step_keys=payload.steps, reset_downstream=True, input_data=payload.input, actor=user.email
    )
    db.commit()
    db.refresh(run)
    return {"id": run.id, "status": run.status, **result}


@router.post("/{run_id}/replay", response_model=RunSummary, status_code=status.HTTP_201_CREATED)
def replay_run(run_id: str, payload: ReplayRunRequest, user: CurrentUser, db: DbSession) -> dict:
    """Replay a run with edited input (Stage H3).

    Creates a brand-new run linked to the original via ``parent_run_id``,
    using the original's published version (or the requested one) with the
    caller's replacement input. The original run is untouched.
    """
    from app.services import quota_service, workflow_service

    original = run_service.get_run(db, run_id, user.id)
    workflow = workflow_service.get_workflow(db, original.workflow_id, user.id)
    workflow_service.require_workflow_operate(db, workflow, user.id)
    quota_service.check_run_quota(db, triggered_by=user.email)
    run, _ = run_service.create_run(
        db,
        workflow,
        version=payload.version or original.version,
        input_data=payload.input,
        trigger="replay",
        triggered_by=user.email,
        parent_run_id=original.id,
        max_parallel=original.max_parallel,
        priority=original.priority,
        queue_name=original.queue_name,
    )
    db.commit()
    db.refresh(run)
    return run_service.run_summary_view(db, run, workflow_name=workflow.name)


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_run(run_id: str, user: CurrentUser, db: DbSession) -> None:
    """Delete a finished run. Running runs must be cancelled first."""
    run = run_service.get_run(db, run_id, user.id)
    from app.core.errors import Conflict

    if run.status not in run_service.TERMINAL_RUN_STATUSES:
        raise Conflict("Cancel this run before deleting it", code="run_active")
    db.delete(run)
    db.commit()


@router.get("/{run_id}/summary", response_model=RunSummary)
def get_run_summary(run_id: str, user: CurrentUser, db: DbSession) -> dict:
    run = run_service.get_run(db, run_id, user.id)
    workflow = db.get(Workflow, run.workflow_id)
    return run_service.run_summary_view(db, run, workflow_name=workflow.name if workflow else "")


@router.get("/{run_id}/stats")
def run_step_stats(run_id: str, user: CurrentUser, db: DbSession) -> dict:
    """Attempt-level counters for one run, used by the run detail header."""
    run = run_service.get_run(db, run_id, user.id)
    rows = db.execute(
        select(StepRun.status, func.count()).where(StepRun.run_id == run.id).group_by(StepRun.status)
    ).all()
    attempts = int(
        db.scalar(
            select(func.count()).select_from(StepAttempt).join(StepRun, StepRun.id == StepAttempt.step_run_id).where(StepRun.run_id == run.id)
        )
        or 0
    )
    retried = int(
        db.scalar(select(func.count()).select_from(StepRun).where(StepRun.run_id == run.id, StepRun.attempts > 1)) or 0
    )
    return {
        "run_id": run.id,
        "status": run.status,
        "step_counts": {status: int(count) for status, count in rows},
        "total_attempts": attempts,
        "retried_steps": retried,
        **run_service.ai_usage_for_run(db, run.id),
    }


__all__ = ["router"]
