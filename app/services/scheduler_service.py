"""Recurring schedules and the background scheduler loop.

Duplicate protection works on two levels:

1. **Slot identity.** Each occurrence is identified by its UTC minute
   (``slot_key``). The scheduler only fires when ``next_run_at`` is due and the
   slot differs from ``last_fired_slot``, so a restart cannot double-fire.
2. **Run idempotency.** The created run carries an idempotency key derived from
   the schedule id and slot, and the unique index on
   ``(workflow_id, idempotency_key)`` rejects a duplicate at the database level
   even if two scheduler replicas race.

Overlap policy decides what happens when the previous run is still active:
``skip`` (default) records a skip, ``allow`` starts anyway, and
``cancel_previous`` cancels the in-flight run first.
"""
from __future__ import annotations

import threading
import hashlib
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.core import cron
from app.core.errors import Conflict, Invalid, NotFound
from app.core.logging import get_logger
from app.core.metrics import counter, gauge
from app.database import advisory_lock, session_scope
from app.models.event import EventType
from app.models.run import WorkflowRun
from app.models.schedule import ScheduleBackfill, WorkflowSchedule
from app.models.workflow import Workflow
from app.services import event_service, run_service

logger = get_logger("app.scheduler")

TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled"}
# Stable PostgreSQL advisory-lock key so only one scheduler replica ticks at a time.
SCHEDULER_LOCK_KEY = 8_150_231


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def validate_schedule_inputs(cron_expression: str, timezone_name: str) -> tuple[str, str]:
    error = cron.validate_expression(cron_expression)
    if error:
        raise Invalid(error, code="invalid_cron", details={"field": "cron_expression"})
    try:
        cron.resolve_timezone(timezone_name)
    except cron.CronError as exc:
        raise Invalid(str(exc), code="invalid_timezone", details={"field": "timezone"}) from exc
    return cron_expression.strip(), timezone_name or "UTC"


def compute_next_run(schedule: WorkflowSchedule, *, after: datetime | None = None) -> datetime | None:
    try:
        tz = cron.resolve_timezone(schedule.timezone)
        cursor = _aware(after) or datetime.now(timezone.utc)
        for _ in range(20_000):
            candidate = cron.next_run_at(schedule.cron_expression, schedule.timezone, after=cursor)
            if _calendar_skips(schedule, candidate, tz):
                cursor = candidate
                continue
            jitter = int(getattr(schedule, "jitter_seconds", 0) or 0)
            if jitter:
                following = cron.next_run_at(schedule.cron_expression, schedule.timezone, after=candidate)
                max_safe_jitter = max(0, int((following - candidate).total_seconds()) - 1)
                jitter = min(jitter, max_safe_jitter)
                seed = hashlib.sha256(f"{schedule.id}:{cron.slot_key(candidate)}".encode()).hexdigest()[:16]
                seconds = random.Random(int(seed, 16)).randint(0, jitter)
                candidate += timedelta(seconds=seconds)
            return candidate.astimezone(timezone.utc).replace(tzinfo=None)
        return None
    except cron.CronError as exc:  # pragma: no cover - guarded at creation
        logger.warning("Schedule has an invalid expression", extra={"schedule_id": schedule.id, "error": str(exc)})
        return None


def _logical_slot(schedule: WorkflowSchedule, occurrence: datetime) -> datetime:
    """Return the nominal cron instant for a possibly jittered execution time."""
    tz = cron.resolve_timezone(schedule.timezone)
    cron_schedule = cron.CronSchedule(schedule.cron_expression)
    occurrence_aware = _aware(occurrence)
    assert occurrence_aware is not None
    nominal = cron_schedule.previous_before(occurrence_aware + timedelta(minutes=1), tz)
    return nominal.astimezone(timezone.utc).replace(tzinfo=None)


def _calendar_skips(schedule: WorkflowSchedule, candidate: datetime, tz) -> bool:
    local = candidate.astimezone(tz)
    if schedule.skip_weekends and local.weekday() >= 5:
        return True
    if local.date().isoformat() in set(schedule.skip_dates or []):
        return True
    for window in schedule.pause_windows or []:
        try:
            start = datetime.fromisoformat(window["start"])
            end = datetime.fromisoformat(window["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        if end.tzinfo is None:
            end = end.replace(tzinfo=tz)
        if start <= local < end:
            return True
    return False


def _validate_calendar_settings(*, timezone_name: str, skip_dates: list[str], pause_windows: list[dict[str, str]], jitter_seconds: int) -> None:
    if not 0 <= int(jitter_seconds) <= 3600:
        raise Invalid("jitter_seconds must be between 0 and 3600", code="schedule_jitter_invalid")
    for value in skip_dates:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise Invalid("skip_dates must contain YYYY-MM-DD dates", code="schedule_date_invalid") from exc
    tz = cron.resolve_timezone(timezone_name)
    for window in pause_windows:
        if set(window) != {"start", "end"}:
            raise Invalid("Each pause window must have start and end timestamps", code="schedule_pause_window_invalid")
        try:
            start = datetime.fromisoformat(window["start"])
            end = datetime.fromisoformat(window["end"])
        except (TypeError, ValueError) as exc:
            raise Invalid("Pause window timestamps must be ISO 8601", code="schedule_pause_window_invalid") from exc
        start = start.replace(tzinfo=tz) if start.tzinfo is None else start
        end = end.replace(tzinfo=tz) if end.tzinfo is None else end
        if end <= start:
            raise Invalid("Pause window end must be later than its start", code="schedule_pause_window_invalid")


def preview(cron_expression: str, timezone_name: str, count: int = 5) -> dict[str, Any]:
    error = cron.validate_expression(cron_expression)
    if error:
        return {"valid": False, "error": error, "description": "", "timezone": timezone_name, "next_runs": []}
    try:
        tz = cron.resolve_timezone(timezone_name)
    except cron.CronError as exc:
        return {"valid": False, "error": str(exc), "description": "", "timezone": timezone_name, "next_runs": []}
    schedule = cron.CronSchedule(cron_expression)
    runs = cron.upcoming_runs(cron_expression, timezone_name, count)
    return {
        "valid": True,
        "error": None,
        "description": schedule.describe(),
        "timezone": str(getattr(tz, "key", timezone_name) or "UTC"),
        "next_runs": runs,
    }


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def get_schedule(db: Session, schedule_id: str, owner_id: str) -> WorkflowSchedule:
    schedule = db.get(WorkflowSchedule, schedule_id)
    if schedule is None or schedule.owner_id != owner_id:
        raise NotFound("Schedule not found", code="schedule_not_found")
    return schedule


def list_schedules(
    db: Session,
    owner_id: str,
    *,
    workflow_id: str | None = None,
    enabled: bool | None = None,
) -> list[WorkflowSchedule]:
    query = select(WorkflowSchedule).where(WorkflowSchedule.owner_id == owner_id)
    if workflow_id:
        query = query.where(WorkflowSchedule.workflow_id == workflow_id)
    if enabled is not None:
        query = query.where(WorkflowSchedule.enabled.is_(enabled))
    return list(db.scalars(query.order_by(WorkflowSchedule.created_at.desc())).all())


def create_schedule(db: Session, owner_id: str, workflow: Workflow, payload: Any) -> WorkflowSchedule:
    expression, timezone_name = validate_schedule_inputs(payload.cron_expression, payload.timezone)
    _validate_calendar_settings(
        timezone_name=timezone_name,
        skip_dates=payload.skip_dates,
        pause_windows=payload.pause_windows,
        jitter_seconds=payload.jitter_seconds,
    )
    existing = db.scalar(
        select(WorkflowSchedule).where(
            WorkflowSchedule.workflow_id == workflow.id,
            WorkflowSchedule.cron_expression == expression,
            WorkflowSchedule.timezone == timezone_name,
        )
    )
    if existing is not None:
        raise Conflict("This workflow already has an identical schedule", code="duplicate_schedule")
    version = payload.version or workflow.latest_version
    if version < 1:
        raise Conflict("Publish this workflow before scheduling it", code="no_published_version")
    schedule = WorkflowSchedule(
        workflow_id=workflow.id,
        owner_id=owner_id,
        name=payload.name or f"{workflow.name} schedule",
        cron_expression=expression,
        timezone=timezone_name,
        enabled=payload.enabled,
        # Pin the resolved version so the schedule always targets a published revision.
        version=version,
        input_data=payload.input,
        overlap_policy=payload.overlap_policy,
        catchup=payload.catchup,
        data_interval_seconds=payload.data_interval_seconds,
        jitter_seconds=payload.jitter_seconds,
        skip_weekends=payload.skip_weekends,
        skip_dates=payload.skip_dates,
        pause_windows=payload.pause_windows,
    )
    schedule.next_run_at = compute_next_run(schedule)
    db.add(schedule)
    db.flush()
    event_service.emit(
        db, EventType.SCHEDULE_CREATED, workflow_id=workflow.id,
        message=f"Schedule '{schedule.name}' created ({expression} {timezone_name})",
        payload={"schedule_id": schedule.id, "cron": expression, "timezone": timezone_name, "enabled": schedule.enabled},
    )
    return schedule


def update_schedule(db: Session, schedule: WorkflowSchedule, payload: Any) -> WorkflowSchedule:
    data = payload.model_dump(exclude_unset=True)
    if "cron_expression" in data or "timezone" in data:
        expression, timezone_name = validate_schedule_inputs(
            data.get("cron_expression") or schedule.cron_expression,
            data.get("timezone") or schedule.timezone,
        )
        schedule.cron_expression = expression
        schedule.timezone = timezone_name
        schedule.last_fired_slot = None
    if "name" in data and data["name"] is not None:
        schedule.name = data["name"]
    if "version" in data:
        schedule.version = data["version"]
    if "input" in data and data["input"] is not None:
        schedule.input_data = data["input"]
    if "overlap_policy" in data and data["overlap_policy"] is not None:
        schedule.overlap_policy = data["overlap_policy"]
    if "catchup" in data and data["catchup"] is not None:
        schedule.catchup = data["catchup"]
    if "data_interval_seconds" in data:
        schedule.data_interval_seconds = data["data_interval_seconds"]
    if "jitter_seconds" in data and data["jitter_seconds"] is not None:
        schedule.jitter_seconds = data["jitter_seconds"]
    if "skip_weekends" in data and data["skip_weekends"] is not None:
        schedule.skip_weekends = data["skip_weekends"]
    if "skip_dates" in data and data["skip_dates"] is not None:
        schedule.skip_dates = data["skip_dates"]
    if "pause_windows" in data and data["pause_windows"] is not None:
        schedule.pause_windows = data["pause_windows"]
    _validate_calendar_settings(
        timezone_name=schedule.timezone,
        skip_dates=schedule.skip_dates or [],
        pause_windows=schedule.pause_windows or [],
        jitter_seconds=schedule.jitter_seconds,
    )
    if "enabled" in data and data["enabled"] is not None:
        schedule.enabled = bool(data["enabled"])
    schedule.next_run_at = compute_next_run(schedule) if schedule.enabled else None
    schedule.updated_at = utcnow()
    db.flush()
    event_service.emit(
        db, EventType.SCHEDULE_UPDATED, workflow_id=schedule.workflow_id,
        message=f"Schedule '{schedule.name}' updated",
        payload={"schedule_id": schedule.id, "enabled": schedule.enabled, "cron": schedule.cron_expression, "timezone": schedule.timezone},
    )
    return schedule


def delete_schedule(db: Session, schedule: WorkflowSchedule) -> None:
    event_service.emit(
        db, EventType.SCHEDULE_DELETED, workflow_id=schedule.workflow_id,
        message=f"Schedule '{schedule.name}' deleted",
        payload={"schedule_id": schedule.id},
    )
    db.delete(schedule)


# --------------------------------------------------------------------------- #
# Firing
# --------------------------------------------------------------------------- #
def fire_schedule(
    db: Session,
    schedule: WorkflowSchedule,
    *,
    slot: datetime | None = None,
    actor: str | None = None,
    apply_overlap: bool = True,
    backfill_id: str | None = None,
) -> dict[str, Any]:
    """Create a run for one schedule occurrence.

    Returns ``{"status": "started"|"skipped"|"failed", "run_id": ...}``.
    """
    from app.services import workflow_service

    workflow = db.get(Workflow, schedule.workflow_id)
    if workflow is None or workflow.archived:
        raise NotFound("The workflow for this schedule no longer exists", code="workflow_not_found")

    occurrence = slot or _aware(schedule.next_run_at) or datetime.now(timezone.utc)
    logical_date = _logical_slot(schedule, occurrence)
    # Keep idempotency identity tied to the execution occurrence. Callers that
    # explicitly fire a schedule at distinct manual timestamps must not collapse
    # into the same nominal cron slot.
    slot_identity = cron.slot_key(occurrence)
    key = f"sched:{schedule.id}:{slot_identity}"

    # Duplicate guard 1: the slot already fired.
    if schedule.last_fired_slot == slot_identity:
        return {"status": "skipped", "run_id": None, "message": "That occurrence has already run"}

    # Overlap handling.
    if apply_overlap and schedule.overlap_policy in {"skip", "cancel_previous"}:
        active = db.scalars(
            select(WorkflowRun).where(
                WorkflowRun.workflow_id == schedule.workflow_id,
                WorkflowRun.status.not_in(TERMINAL_RUN_STATUSES),
            )
        ).all()
        active = [run for run in active if run.schedule_id == schedule.id or schedule.overlap_policy == "skip"]
        if active:
            if schedule.overlap_policy == "cancel_previous":
                for run in active:
                    run_service.request_cancel(db, run, actor=actor)
            else:
                schedule.last_fired_slot = slot_identity
                schedule.last_run_at = occurrence.replace(tzinfo=None)
                schedule.next_run_at = compute_next_run(schedule)
                schedule.last_error = "Skipped because the previous run was still active"
                event_service.emit(
                    db, EventType.SCHEDULE_SKIPPED, workflow_id=workflow.id,
                    level="warning",
                    message="Skipped: the previous run is still active",
                    payload={"schedule_id": schedule.id, "active_run_id": active[0].id, "slot": key},
                )
                return {"status": "skipped", "run_id": active[0].id, "message": "The previous run is still active"}

    # Duplicate guard 2: the unique idempotency index.
    interval_seconds = schedule.data_interval_seconds
    interval_end = logical_date
    if interval_seconds:
        interval_start = logical_date - timedelta(seconds=interval_seconds)
    else:
        previous = cron.CronSchedule(schedule.cron_expression).previous_before(
            _aware(logical_date),
            cron.resolve_timezone(schedule.timezone),
        )
        interval_start = previous.astimezone(timezone.utc).replace(tzinfo=None)
    input_data = dict(schedule.input_data or {})
    run, replayed = run_service.create_run(
        db,
        workflow,
        version=schedule.version,
        input_data=input_data,
        idempotency_key=key,
        trigger="schedule",
        triggered_by=actor or f"schedule:{schedule.id}",
        schedule_id=schedule.id,
        logical_date=logical_date,
        interval_start=interval_start,
        interval_end=interval_end,
        backfill_id=backfill_id,
    )
    schedule.run_count += 0 if replayed else 1
    if backfill_id is None:
        schedule.last_fired_slot = slot_identity
        schedule.last_run_at = occurrence.replace(tzinfo=None)
        schedule.last_run_id = run.id
        schedule.last_error = None
        schedule.next_run_at = compute_next_run(schedule)
    db.flush()
    counter("orchestrator_schedule_fires_total")
    event_service.emit(
        db, EventType.SCHEDULE_TRIGGERED, workflow_id=workflow.id,
        message=f"Schedule '{schedule.name}' started run {run.id[:8]}",
        payload={"schedule_id": schedule.id, "run_id": run.id, "slot": key, "version": run.version, "replayed": replayed, "backfill_id": backfill_id},
    )
    return {"status": "started", "run_id": run.id, "message": "Run started"}


def create_backfill(
    db: Session,
    schedule: WorkflowSchedule,
    owner_id: str,
    *,
    start: datetime,
    end: datetime,
    concurrency_limit: int,
) -> ScheduleBackfill:
    start_aware = _aware(start)
    end_aware = _aware(end)
    if start_aware is None or end_aware is None or end_aware <= start_aware:
        raise Invalid("Backfill end must be later than start", code="backfill_range_invalid")
    if end_aware - start_aware > timedelta(days=366):
        raise Invalid("A backfill range may not exceed 366 days", code="backfill_range_too_large")
    candidate = compute_next_run(schedule, after=start_aware - timedelta(minutes=1))
    if candidate is None or _logical_slot(schedule, candidate) >= end_aware.replace(tzinfo=None):
        raise Invalid("The requested backfill range contains no schedule slots", code="backfill_empty")
    job = ScheduleBackfill(
        schedule_id=schedule.id,
        owner_id=owner_id,
        start_at=start_aware.astimezone(timezone.utc).replace(tzinfo=None),
        end_at=end_aware.astimezone(timezone.utc).replace(tzinfo=None),
        next_slot_at=candidate,
        concurrency_limit=concurrency_limit,
        status="running",
        created_at=utcnow(),
    )
    db.add(job)
    db.flush()
    event_service.emit(
        db, "schedule.backfill_created", workflow_id=schedule.workflow_id,
        message="Schedule backfill created",
        payload={"schedule_id": schedule.id, "backfill_id": job.id, "start": job.start_at.isoformat(), "end": job.end_at.isoformat(), "concurrency_limit": concurrency_limit},
        actor=owner_id,
    )
    pump_backfills(db, limit=10)
    db.flush()
    return job


def pump_backfills(db: Session, *, limit: int = 100) -> int:
    jobs = list(
        db.scalars(
            select(ScheduleBackfill)
            .where(ScheduleBackfill.status.in_(("running", "draining")))
            .order_by(ScheduleBackfill.created_at)
            .limit(limit)
        ).all()
    )
    started = 0
    for job in jobs:
        schedule = db.scalar(
            select(WorkflowSchedule).where(
                WorkflowSchedule.id == job.schedule_id,
                WorkflowSchedule.owner_id == job.owner_id,
            )
        )
        if schedule is None:
            job.status = "failed"
            job.finished_at = utcnow()
            continue
        active = int(
            db.scalar(
                select(func.count()).select_from(WorkflowRun).where(
                    WorkflowRun.backfill_id == job.id,
                    WorkflowRun.status.not_in(TERMINAL_RUN_STATUSES),
                )
            )
            or 0
        )
        while job.status == "running" and active < job.concurrency_limit:
            slot = _aware(job.next_slot_at)
            if slot is None or _logical_slot(schedule, slot) >= job.end_at.replace(tzinfo=None):
                job.status = "draining" if active else "completed"
                if job.status == "completed":
                    job.finished_at = utcnow()
                break
            result = fire_schedule(
                db,
                schedule,
                slot=slot,
                actor=f"backfill:{job.id}",
                apply_overlap=False,
                backfill_id=job.id,
            )
            following = compute_next_run(schedule, after=slot)
            job.next_slot_at = following
            if result["status"] == "started":
                started += 1
                active += 1
        if job.status == "running" and (
            job.next_slot_at is None or _logical_slot(schedule, job.next_slot_at) >= job.end_at.replace(tzinfo=None)
        ):
            job.status = "draining" if active else "completed"
            if job.status == "completed":
                job.finished_at = utcnow()
        if job.status == "draining" and active == 0:
            job.status = "completed"
            job.finished_at = utcnow()
    return started


def due_schedules(db: Session, *, limit: int | None = None) -> list[WorkflowSchedule]:
    now = utcnow()
    return list(
        db.scalars(
            select(WorkflowSchedule)
            .where(WorkflowSchedule.enabled.is_(True), WorkflowSchedule.next_run_at.is_not(None), WorkflowSchedule.next_run_at <= now)
            .order_by(WorkflowSchedule.next_run_at)
            .limit(limit or settings.scheduler_batch_size)
        ).all()
    )


def tick(db: Session, *, actor: str = "scheduler") -> int:
    """Fire every schedule that is due. Returns the number of runs started."""
    if not advisory_lock(db, SCHEDULER_LOCK_KEY):
        return 0
    run_service.settle_due_runs(db)
    started = pump_backfills(db)
    for schedule in due_schedules(db):
        try:
            result = fire_schedule(db, schedule, actor=actor)
            if result["status"] == "started":
                started += 1
        except Exception as exc:  # keep the loop alive; record why
            db.rollback()
            logger.exception("Schedule fire failed", extra={"schedule_id": schedule.id})
            record = db.get(WorkflowSchedule, schedule.id)
            if record is not None:
                record.last_error = f"{type(exc).__name__}: {exc}"[:500]
                record.next_run_at = compute_next_run(record)
                event_service.emit(
                    db, EventType.SCHEDULE_FAILED, workflow_id=record.workflow_id, level="error",
                    message=f"Schedule '{record.name}' could not start a run",
                    payload={"schedule_id": record.id, "error": str(exc)},
                )
                db.commit()
    if started:
        logger.info("Scheduler started runs", extra={"count": started})
    return started


def scheduler_metrics(db: Session) -> None:
    gauge("orchestrator_schedules_due", float(len(due_schedules(db, limit=1000))))


class SchedulerLoop:
    """Background scheduler thread for single-process deployments.

    For multiple API replicas, run the scheduler as its own process instead
    (``python -m app.scheduler``) — the advisory lock makes that safe either way.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_tick_at: datetime | None = None
        self.last_error: str | None = None

    def _loop(self) -> None:
        logger.info("Scheduler loop started", extra={"interval_seconds": settings.scheduler_interval_seconds})
        while not self._stop.wait(settings.scheduler_interval_seconds):
            try:
                with session_scope() as db:
                    tick(db)
                    scheduler_metrics(db)
                self.last_tick_at = utcnow()
                self.last_error = None
            except Exception as exc:  # pragma: no cover - resilience path
                self.last_error = str(exc)
                logger.warning("Scheduler cycle failed", extra={"error": str(exc)})
        logger.info("Scheduler loop stopped")

    def start(self) -> None:
        if not settings.scheduler_enabled:
            logger.info("Scheduler disabled by configuration")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


loop = SchedulerLoop()


def schedule_view(db: Session, schedule: WorkflowSchedule, *, workflow_name: str = "", upcoming: int = 3) -> dict[str, Any]:
    last_run = db.get(WorkflowRun, schedule.last_run_id) if schedule.last_run_id else None
    try:
        description = cron.CronSchedule(schedule.cron_expression).describe()
    except cron.CronError:
        description = schedule.cron_expression
    try:
        next_runs = []
        cursor = datetime.now(timezone.utc)
        for _ in range(upcoming):
            occurrence = compute_next_run(schedule, after=cursor)
            if occurrence is None:
                break
            next_runs.append(occurrence)
            cursor = _aware(occurrence)
    except cron.CronError:
        next_runs = []
    return {
        "id": schedule.id,
        "workflow_id": schedule.workflow_id,
        "workflow_name": workflow_name,
        "name": schedule.name,
        "cron_expression": schedule.cron_expression,
        "cron_description": description,
        "timezone": schedule.timezone,
        "enabled": schedule.enabled,
        "version": schedule.version,
        "effective_version": schedule.version or 0,
        "input": schedule.input_data or {},
        "overlap_policy": schedule.overlap_policy,
        "catchup": schedule.catchup,
        "data_interval_seconds": schedule.data_interval_seconds,
        "jitter_seconds": schedule.jitter_seconds,
        "skip_weekends": schedule.skip_weekends,
        "skip_dates": schedule.skip_dates or [],
        "pause_windows": schedule.pause_windows or [],
        "next_run_at": schedule.next_run_at,
        "last_run_at": schedule.last_run_at,
        "last_run_id": schedule.last_run_id,
        "last_status": last_run.status if last_run else None,
        "run_count": schedule.run_count,
        "last_error": schedule.last_error,
        "upcoming": next_runs,
        "created_at": schedule.created_at,
        "updated_at": schedule.updated_at,
    }


def schedule_stats(db: Session, owner_id: str) -> dict[str, int]:
    enabled = int(
        db.scalar(
            select(func.count())
            .select_from(WorkflowSchedule)
            .where(WorkflowSchedule.owner_id == owner_id, WorkflowSchedule.enabled.is_(True))
        )
        or 0
    )
    due = int(
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
    return {"enabled": enabled, "due": due}


__all__ = [
    "SchedulerLoop",
    "compute_next_run",
    "create_backfill",
    "create_schedule",
    "delete_schedule",
    "due_schedules",
    "fire_schedule",
    "get_schedule",
    "list_schedules",
    "loop",
    "preview",
    "pump_backfills",
    "schedule_stats",
    "schedule_view",
    "tick",
    "update_schedule",
    "validate_schedule_inputs",
]
