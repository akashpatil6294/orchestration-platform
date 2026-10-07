"""Run lifecycle: creation, dependency-aware settlement, cancellation, retry.

Design notes
------------
* A run is **pinned** to one immutable published version; the version's
  definition is copied into ``StepRun`` rows at creation time so a later publish
  cannot change a run in flight.
* ``settle_run`` is the single place that decides which steps are ready, which
  are skipped because a dependency cannot succeed, and when the run itself is
  finished. It is idempotent and safe to call after any state change.
* Dispatch messages are written to the transactional outbox in the *same*
  transaction as the state change, so a queue message can never describe a
  state that was rolled back.
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.core import dag
from app.core.dataflow import DataReferenceError, resolve_foreach_templates, resolve_templates
from app.core.engine import cache_key_material, foreach_child_key, join_decision
from app.core.expr import evaluate as evaluate_expression, is_truthy
from app.core.errors import Conflict, Invalid, NotFound
from app.core.logging import get_logger
from app.core.pagination import check_run_input
from app.models.event import EventType
from app.models.base import utc as ensure_utc
from app.models.run import OutboxMessage, StepAttempt, StepCacheEntry, StepRun, WorkflowRun
from app.models.workflow import Workflow, WorkflowVersion
from app.services import event_service
from app.services.outbox_service import TOPIC_TASK_READY, enqueue

logger = get_logger("app.runs")

TERMINAL_STEP_STATUSES = {"succeeded", "failed", "cancelled", "skipped"}
TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled"}
ACTIVE_RUN_STATUSES = {"queued", "running", "cancelling"}
ACTIVE_STEP_STATUSES = {"pending", "running", "retrying", "waiting_approval", "waiting_children", "waiting_subworkflow", "waiting_compensation"}
CLAIMABLE_STEP_STATUSES = {"pending", "retrying"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Coercion helpers
#
# Step output is user-shaped data: numbers can arrive as strings, as
# observability redaction placeholders (``'***redacted***'``), as ``None``,
# or as booleans. A single malformed value must never 500 an aggregate.
# --------------------------------------------------------------------------- #
def _safe_int(value: Any, default: int = 0) -> int:
    """Coerce a value to int, treating anything unparseable as ``default``.

    Step ``ai_usage`` payloads may contain redaction placeholders
    (e.g. ``'***redacted***'``) from the observability layer. We must not let
    one malformed row 500 the entire run-stats endpoint.
    """
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Coerce a value to float, treating anything unparseable as ``default``."""
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #
def get_run(db: Session, run_id: str, owner_id: str) -> WorkflowRun:
    from app.services import workflow_service

    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise NotFound("Run not found", code="run_not_found")
    try:
        workflow_service.get_workflow(db, run.workflow_id, owner_id)
    except NotFound:
        raise NotFound("Run not found", code="run_not_found")
    return run


RUN_SORT_CHOICES = ("newest", "oldest", "longest", "status")


def list_runs(
    db: Session,
    owner_id: str,
    *,
    workflow_id: str | None = None,
    status: str | None = None,
    trigger: str | None = None,
    search: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    sort: str = "newest",
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[WorkflowRun], int]:
    from app.services import team_service

    team_ids = team_service.user_team_ids(db, owner_id)
    query = (
        select(WorkflowRun)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(
            (Workflow.owner_id == owner_id)
            | (Workflow.team_id.in_(team_ids) if team_ids else False)
        )
    )
    if workflow_id:
        query = query.where(WorkflowRun.workflow_id == workflow_id)
    if status:
        query = query.where(WorkflowRun.status == status)
    if trigger:
        query = query.where(WorkflowRun.trigger == trigger)
    if since:
        query = query.where(WorkflowRun.created_at >= since)
    if until:
        query = query.where(WorkflowRun.created_at < until)
    if search:
        needle = f"%{search.strip().lower()}%"
        query = query.where(func.lower(Workflow.name).like(needle) | func.lower(WorkflowRun.id).like(needle))
    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    query = query.order_by(*_run_order_clause(db, sort))
    rows = list(db.scalars(query.limit(limit).offset(offset)).all())
    return rows, total


def ai_usage_for_run(db: Session, run_id: str) -> dict[str, Any]:
    """Aggregate per-step AI token usage and cost for one run.

    Each AI connector step stores an ``ai_usage`` object in its output; the
    run total sums tokens and cost across steps. Steps without AI usage
    contribute nothing.

    Values are coerced defensively: a single row whose ``ai_usage`` contains
    a non-numeric placeholder (e.g. ``'***redacted***'`` written by the
    observability layer) is treated as ``0`` rather than crashing the
    run-stats endpoint.
    """
    steps = steps_for(db, run_id)
    totals: dict[str, Any] = {}
    steps_with_usage = 0
    for step in steps:
        usage = (step.output_data or {}).get("ai_usage") if isinstance(step.output_data, dict) else None
        if not isinstance(usage, dict):
            continue
        prompt_tokens = _safe_int(usage.get("prompt_tokens"))
        completion_tokens = _safe_int(usage.get("completion_tokens"))
        total_tokens = _safe_int(usage.get("total_tokens"))
        cost_usd = _safe_float(usage.get("cost_usd"))
        if not (prompt_tokens or completion_tokens or total_tokens):
            continue
        steps_with_usage += 1
        model = str(usage.get("model") or "unknown")
        bucket = totals.setdefault(
            model,
            {"model": model, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "steps": 0},
        )
        bucket["prompt_tokens"] += prompt_tokens
        bucket["completion_tokens"] += completion_tokens
        bucket["total_tokens"] += total_tokens
        bucket["steps"] += 1
        if cost_usd or "cost_usd" in bucket:
            bucket["cost_usd"] = round(_safe_float(bucket.get("cost_usd")) + cost_usd, 6)
    if not totals:
        return {"ai_usage": None}
    return {"ai_usage": {"models": sorted(totals.values(), key=lambda item: item["model"]), "steps": steps_with_usage}}


def _run_order_clause(db: Session, sort: str) -> tuple[Any, ...]:
    """Orderings exposed to the runs list; every choice pins created_at as a tiebreaker."""
    if sort == "oldest":
        return (WorkflowRun.created_at.asc(), WorkflowRun.id.asc())
    if sort == "status":
        return (WorkflowRun.status.asc(), WorkflowRun.created_at.desc())
    if sort == "longest":
        # Finished runs first by elapsed time; unfinished runs sort last on both dialects.
        if db.bind is not None and db.bind.dialect.name == "sqlite":
            duration = func.julianday(WorkflowRun.finished_at) - func.julianday(WorkflowRun.started_at)
        else:
            duration = WorkflowRun.finished_at - WorkflowRun.started_at
        return (duration.desc().nulls_last(), WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
    return (WorkflowRun.created_at.desc(), WorkflowRun.id.desc())


def steps_for(db: Session, run_id: str) -> list[StepRun]:
    return list(db.scalars(select(StepRun).where(StepRun.run_id == run_id).order_by(StepRun.created_at, StepRun.step_key)).all())


def step_map(steps: Iterable[StepRun]) -> dict[str, StepRun]:
    return {step.step_key: step for step in steps}


def workflow_input_for_run(run: WorkflowRun) -> dict[str, Any]:
    """Return workflow input plus non-persisted schedule interval metadata."""
    value = dict(run.input_data or {})
    if run.logical_date is not None:
        date_value = ensure_utc(run.logical_date).isoformat().replace("+00:00", "Z")
        start_value = ensure_utc(run.interval_start).isoformat().replace("+00:00", "Z") if run.interval_start else None
        end_value = ensure_utc(run.interval_end).isoformat().replace("+00:00", "Z") if run.interval_end else None
        value.setdefault("logical_date", date_value)
        if start_value is not None:
            value.setdefault("interval_start", start_value)
            value.setdefault("interval_end", end_value)
        value.setdefault("data_interval", {"logical_date": date_value, "interval_start": start_value, "interval_end": end_value})
    return value


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #
def create_run(
    db: Session,
    workflow: Workflow,
    *,
    version: int | None = None,
    input_data: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    trigger: str = "manual",
    triggered_by: str | None = None,
    max_parallel: int | None = None,
    priority: int = 0,
    queue_name: str = "default",
    schedule_id: str | None = None,
    parent_run_id: str | None = None,
    parent_step_run_id: str | None = None,
    nesting_depth: int = 0,
    logical_date: datetime | None = None,
    interval_start: datetime | None = None,
    interval_end: datetime | None = None,
    backfill_id: str | None = None,
    is_test: bool = False,
    draft_definition: dict[str, Any] | None = None,
) -> tuple[WorkflowRun, bool]:
    """Create a run pinned to a published version.

    When ``draft_definition`` is given (builder test runs), the draft executes
    directly without a published version and ``is_test`` marks the run so
    triggers, notifications and production quotas are skipped.

    Returns ``(run, replayed)``. When ``idempotency_key`` is supplied and a run
    with the same key already exists for this workflow, that run is returned and
    nothing new is created.
    """
    payload = input_data or {}
    check_run_input(payload)

    if draft_definition is not None:
        definition = draft_definition
        version_no = 0
        steps = definition.get("steps", [])
    else:
        version_no = version or workflow.latest_version
        if version_no < 1:
            raise Conflict("Publish this workflow before starting a run", code="no_published_version")
        published = db.scalar(
            select(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow.id, WorkflowVersion.version == version_no)
        )
        if published is None:
            raise NotFound(f"Version {version_no} of this workflow does not exist", code="version_not_found")
        definition = published.definition
        steps = definition.get("steps", [])

    if idempotency_key:
        existing = db.scalar(
            select(WorkflowRun).where(
                WorkflowRun.workflow_id == workflow.id, WorkflowRun.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing, True

    if len(steps) > settings.max_steps_per_run:
        raise Invalid(
            f"This workflow has {len(steps)} steps; the per-run limit is {settings.max_steps_per_run}",
            code="too_many_steps",
        )

    # Secret references remain encrypted references in StepRun.input_data. The
    # worker claim path expands them in memory only when dispatching the task.
    resolved_steps = steps

    # Fail fast on missing connections: a run that references an undefined
    # {"$connection": name} would otherwise wedge at claim time.
    from app.services.connection_service import resolve_connection_value
    from app.services.workflow_service import referenced_connection_names

    for connection_name in sorted(referenced_connection_names(definition)):
        try:
            resolve_connection_value(db, owner_id=workflow.owner_id, team_id=workflow.team_id, name=connection_name)
        except Invalid as exc:
            raise Invalid(
                f"Connection '{connection_name}' is not defined — create it on the Connections page first",
                code="connection_missing",
                details={"connection": connection_name},
            ) from exc

    run = WorkflowRun(
        workflow_id=workflow.id,
        version=version_no,
        status="queued",
        trigger=trigger,
        is_test=is_test,
        input_data=payload,
        idempotency_key=idempotency_key,
        max_parallel=max_parallel or workflow.default_max_parallel,
        priority=priority,
        queue_name=queue_name,
        schedule_id=schedule_id,
        triggered_by=triggered_by,
        parent_run_id=parent_run_id,
        parent_step_run_id=parent_step_run_id,
        nesting_depth=nesting_depth,
        logical_date=logical_date,
        interval_start=interval_start,
        interval_end=interval_end,
        backfill_id=backfill_id,
        definition_json=definition,
        deadline_at=(utcnow() + timedelta(seconds=int(definition["timeout_seconds"]))) if definition.get("timeout_seconds") else None,
        sla_deadline_at=(utcnow() + timedelta(seconds=int(definition["sla_seconds"]))) if definition.get("sla_seconds") else None,
    )
    db.add(run)
    from app.core.metrics import counter as _counter

    _counter("orchestrator_tenant_runs_created_total", {"owner_id": workflow.owner_id})
    try:
        db.flush()
    except IntegrityError:
        # A concurrent request won the idempotency race; reuse its run.
        db.rollback()
        if idempotency_key:
            existing = db.scalar(
                select(WorkflowRun).where(
                    WorkflowRun.workflow_id == workflow.id, WorkflowRun.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                return existing, True
        raise Conflict("Could not create the run; please retry", code="run_creation_conflict") from None

    compensation_ids = {spec.get("compensate") for spec in resolved_steps if spec.get("compensate")}
    for spec in resolved_steps:
        compensation_only = spec["id"] in compensation_ids
        step_policy = {
            **{key: value for key, value in spec.items() if key not in {"input", "id", "type", "depends_on"}},
            "has_secrets": bool(_referenced_secret_names(spec)),
        }
        if compensation_only:
            step_policy["compensation_only"] = True
        db.add(
            StepRun(
                run_id=run.id,
                step_key=spec["id"],
                task_type=spec["type"],
                priority=int(spec.get("priority", 0) or priority),
                queue_name=str(spec.get("queue") or queue_name) if spec.get("queue", "default") != "default" else queue_name,
                concurrency_key=spec.get("concurrency_key"),
                concurrency_limit=spec.get("concurrency_limit"),
                input_data=spec.get("input", {}),
                depends_on=list(spec.get("depends_on", [])),
                spec_json=step_policy,
                retry_limit=int(spec.get("retries", 0)),
                backoff_seconds=int(spec.get("backoff_seconds", 2)),
                backoff_multiplier=float(spec.get("backoff_multiplier", 2.0)),
                required=bool(spec.get("required", True)) and not compensation_only,
                timeout_seconds=int(spec.get("timeout_seconds", 300)),
                status="waiting_compensation" if compensation_only else "pending",
                available_at=utcnow(),
            )
        )
    db.flush()

    event_service.emit(
        db,
        EventType.RUN_CREATED,
        run_id=run.id,
        message=f"Run queued on version {version_no}",
        payload={
            "workflow_id": workflow.id,
            "version": version_no,
            "trigger": trigger,
            "step_count": len(steps),
            "max_parallel": run.max_parallel,
        },
        actor=triggered_by,
    )
    settle_run(db, run)
    db.flush()
    return run, False


def _referenced_secret_names(step: dict[str, Any]) -> set[str]:
    from app.services import workflow_service

    return workflow_service.referenced_secret_names({"steps": [step]})


def _input_contains_secret_references(input_data: Any) -> bool:
    from app.services import workflow_service

    return bool(workflow_service.referenced_secret_names({"input": input_data}))


# --------------------------------------------------------------------------- #
# Settlement
# --------------------------------------------------------------------------- #
def settle_run(db: Session, run: WorkflowRun) -> WorkflowRun:
    """Recompute readiness, skip doomed steps and finish the run when possible.

    Idempotent: calling it twice with no intervening state change is a no-op
    (no duplicate events, no status flapping).
    """
    steps = steps_for(db, run.id)
    if not steps:
        return run
    by_key = step_map(steps)
    now = utcnow()
    changed = False
    # Snapshot for alert evaluation: which steps were already failed.
    previously_failed = {s.id for s in steps if s.status == "failed"}
    run_was_terminal = run.status in TERMINAL_RUN_STATUSES

    # Run timeout asks active workers to stop and prevents new dispatches. SLA
    # is an alert condition and does not itself fail a run.
    deadline = ensure_utc(run.deadline_at)
    if deadline is not None and deadline <= ensure_utc(now) and not run.cancel_requested:
        run.error = {"code": "run_timeout", "message": "Workflow run exceeded its configured timeout"}
        run.cancel_requested = True
        run.cancel_requested_at = now
        event_service.emit(db, "run.timeout", run_id=run.id, level="error", message="Run timeout reached; cancelling active steps")
        changed = True
    sla_deadline = ensure_utc(run.sla_deadline_at)
    if sla_deadline is not None and sla_deadline <= ensure_utc(now) and run.sla_breached_at is None:
        run.sla_breached_at = now
        event_service.emit(db, "run.sla_breached", run_id=run.id, level="warning", message="Run exceeded its configured SLA")
        changed = True

    changed = _settle_approval_timeouts(db, run, steps, now) or changed
    changed = _settle_subworkflow_steps(db, run, steps, now) or changed

    # 1. Cancellation: stop everything that has not started.
    if run.cancel_requested:
        for step in steps:
            if (step.spec_json or {}).get("failure_handler") and run.error and run.error.get("code") == "run_timeout":
                continue
            if (step.spec_json or {}).get("compensation_only") and run.error and run.error.get("code") == "run_timeout":
                continue
            if step.status == "waiting_subworkflow" and step.child_run_id:
                child_run = db.get(WorkflowRun, step.child_run_id)
                if child_run is not None and child_run.status not in TERMINAL_RUN_STATUSES and not child_run.cancel_requested:
                    request_cancel(db, child_run, actor="parent-cancellation")
                continue
            if step.status in CLAIMABLE_STEP_STATUSES | {"waiting_approval", "waiting_children", "waiting_compensation"}:
                step.status = "cancelled"
                step.updated_at = now
                event_service.emit(
                    db, EventType.STEP_CANCELLED, run_id=run.id, step=step, message="Cancelled before starting",
                    payload={"reason": "run_cancelled"},
                )
                changed = True

    # Conditions are evaluated by the safe expression parser only after their
    # dependencies are terminal. A false condition is an optional skip.
    changed = _settle_conditions(db, run, steps, by_key, now) or changed

    # 2. Skip descendants according to their declared join mode.
    changed = _skip_doomed(db, run, steps, by_key) or changed

    # Resolve data-dependent inputs as soon as their join is satisfied. Missing
    # runtime fields become a visible failure instead of a broken claim.
    changed = _fail_unresolvable_inputs(db, run, steps, by_key) or changed

    changed = _expand_foreach_ready(db, run, steps, by_key, now) or changed
    steps = steps_for(db, run.id)
    by_key = step_map(steps)
    changed = _settle_fanout_parents(db, run, steps, now) or changed
    steps = steps_for(db, run.id)
    by_key = step_map(steps)
    changed = _skip_doomed(db, run, steps, by_key) or changed

    if _settle_compensations(db, run, steps, now):
        changed = True
        steps = steps_for(db, run.id)
        by_key = step_map(steps)

    if _start_failure_handlers(db, run, steps, now):
        changed = True
        steps = steps_for(db, run.id)
        by_key = step_map(steps)

    # 3. Enqueue steps whose dependencies are all satisfied and that are not
    #    already claimed, respecting the run's concurrency budget.
    _release_ready(db, run, steps, by_key)

    # 4. Decide the run status.
    statuses = {step.status for step in steps}
    if statuses <= TERMINAL_STEP_STATUSES:
        if run.error and run.error.get("code") == "run_timeout":
            _finish_run(db, run, "failed")
        elif run.cancel_requested:
            _finish_run(db, run, "cancelled")
        elif any(
            step.required
            and (step.status == "failed" or (step.status == "skipped" and (step.error or {}).get("code") != "condition_false"))
            for step in steps
        ):
            _finish_run(db, run, "failed")
        else:
            _finish_run(db, run, "succeeded")
    elif run.cancel_requested:
        if run.status != "cancelling":
            run.status = "cancelling"
            changed = True
    elif run.status == "paused":
        pass
    elif any(step.status in {"running", "waiting_approval", "waiting_children", "waiting_subworkflow", "waiting_compensation"} for step in steps):
        if run.status != "running":
            run.status = "running"
            run.started_at = run.started_at or now
            event_service.emit(db, EventType.RUN_STARTED, run_id=run.id, message="Execution started")
            changed = True
    elif run.status not in {"queued", "running"}:
        run.status = "queued"
        changed = True

    if changed:
        run.updated_at = now
    if run.parent_run_id and run.status in TERMINAL_RUN_STATUSES:
        parent = db.get(WorkflowRun, run.parent_run_id)
        if parent is not None and parent.status not in TERMINAL_RUN_STATUSES:
            settle_run(db, parent)
    # Alert evaluation (Stage H4): fire step_failed for newly-failed steps and
    # run_failed when the run newly reaches a terminal failed state. Best-effort:
    # alert failures must never break settlement.
    try:
        from app.services import alert_service

        if not run.is_test:
            fresh_steps = steps_for(db, run.id)
            for step in fresh_steps:
                if step.status == "failed" and step.id not in previously_failed:
                    alert_service.evaluate_step_event(db, step, run)
            if run.status == "failed" and not run_was_terminal:
                # Evaluate run_failed rules with the first failed step as context
                # (or the first step if none failed, e.g. on timeout).
                probe = next((s for s in fresh_steps if s.status == "failed"), fresh_steps[0])
                alert_service.evaluate_step_event(db, probe, run)
    except Exception:  # noqa: BLE001 - defensive; alerts never break dispatch
        pass
    return run


def _settle_conditions(
    db: Session,
    run: WorkflowRun,
    steps: list[StepRun],
    by_key: dict[str, StepRun],
    now: datetime,
) -> bool:
    changed = False
    for step in steps:
        condition = (step.spec_json or {}).get("when")
        if not condition or step.status not in CLAIMABLE_STEP_STATUSES:
            continue
        dependencies = [by_key[dep] for dep in step.depends_on if dep in by_key]
        if any(item.status not in TERMINAL_STEP_STATUSES for item in dependencies):
            continue
        if join_decision(dependencies, mode=(step.spec_json or {}).get("join", "all_success")) != "ready":
            continue
        outputs = {dep: by_key[dep].output_data for dep in step.depends_on if dep in by_key and by_key[dep].status == "succeeded"}
        try:
            result = is_truthy(evaluate_expression(condition, workflow_input=workflow_input_for_run(run), step_outputs=outputs))
        except Exception as exc:
            step.status = "failed"
            step.error = {"code": "condition_evaluation_failed", "message": str(exc), "retryable": False}
            step.finished_at = now
            step.updated_at = now
            event_service.emit(
                db, EventType.STEP_FAILED, run_id=run.id, step=step, level="error",
                message="Step condition could not be evaluated", payload={"code": "condition_evaluation_failed"},
            )
            changed = True
            continue
        if step.task_type == "condition":
            step.status = "succeeded"
            step.output_data = {"value": result}
            step.finished_at = now
            step.updated_at = now
            selected = (step.spec_json or {}).get("if_true" if result else "if_false")
            skipped = (step.spec_json or {}).get("if_false" if result else "if_true")
            event_service.emit(
                db, EventType.STEP_SUCCEEDED, run_id=run.id, step=step,
                message=f"Condition selected the {'true' if result else 'false'} branch",
                payload={"value": result, "selected_step": selected},
            )
            if skipped and skipped in by_key:
                branch = by_key[skipped]
                if branch.status in CLAIMABLE_STEP_STATUSES:
                    branch.status = "skipped"
                    branch.error = {"code": "condition_false", "message": "The other condition branch was not selected"}
                    branch.finished_at = now
                    branch.updated_at = now
                    event_service.emit(
                        db, EventType.STEP_SKIPPED, run_id=run.id, step=branch, level="info",
                        message="Skipped because the other condition branch was selected",
                        payload={"condition_step": step.step_key, "selected_step": selected},
                    )
            changed = True
            continue
        if not result:
            step.status = "skipped"
            step.error = {"code": "condition_false", "message": "Step condition evaluated to false"}
            step.finished_at = now
            step.updated_at = now
            event_service.emit(
                db, EventType.STEP_SKIPPED, run_id=run.id, step=step, level="info",
                message="Skipped because its condition was false", payload={"reason": "condition"},
            )
            changed = True
    return changed


def _expand_foreach_ready(
    db: Session,
    run: WorkflowRun,
    steps: list[StepRun],
    by_key: dict[str, StepRun],
    now: datetime,
) -> bool:
    changed = False
    expanded_count = len(steps)
    for parent in steps:
        spec = parent.spec_json or {}
        foreach = spec.get("foreach")
        if not foreach or parent.status not in CLAIMABLE_STEP_STATUSES or spec.get("foreach_expanded"):
            continue
        dependencies = [by_key[dep] for dep in parent.depends_on if dep in by_key]
        if any(item.status not in TERMINAL_STEP_STATUSES for item in dependencies):
            continue
        if join_decision(dependencies, mode=spec.get("join", "all_success")) != "ready":
            continue
        outputs = {dep: by_key[dep].output_data for dep in parent.depends_on if dep in by_key and by_key[dep].status == "succeeded"}
        try:
            items = resolve_templates(foreach, workflow_input=workflow_input_for_run(run), step_outputs=outputs)
            if not isinstance(items, list):
                raise DataReferenceError("foreach must resolve to an array")
            if expanded_count + len(items) > settings.max_steps_per_run:
                raise DataReferenceError(f"Fan-out would exceed the per-run limit of {settings.max_steps_per_run} steps")
            child_inputs = [resolve_foreach_templates(parent.input_data or {}, item=item, index=index) for index, item in enumerate(items)]
        except (DataReferenceError, TypeError, ValueError) as exc:
            parent.status = "failed"
            parent.error = {"code": "foreach_invalid", "message": str(exc), "retryable": False}
            parent.finished_at = now
            parent.updated_at = now
            spec["foreach_expanded"] = True
            parent.spec_json = spec
            event_service.emit(db, EventType.STEP_FAILED, run_id=run.id, step=parent, level="error", message="Fan-out could not be expanded", payload={"code": "foreach_invalid"})
            changed = True
            continue

        spec["foreach_expanded"] = True
        parent.spec_json = spec
        if not items:
            parent.status = "succeeded"
            parent.output_data = []
            parent.finished_at = now
            parent.updated_at = now
            event_service.emit(db, EventType.STEP_SUCCEEDED, run_id=run.id, step=parent, message="Fan-out had no items", payload={"children": 0})
            changed = True
            continue
        parent.status = "waiting_children"
        parent.started_at = parent.started_at or now
        parent.updated_at = now
        child_spec = {
            "foreach_child": True,
            "max_concurrency": spec.get("max_concurrency"),
            "partial_failure": spec.get("partial_failure", "fail_fast"),
            "cache": spec.get("cache"),
            "has_secrets": bool(spec.get("has_secrets")),
        }
        for index, child_input in enumerate(child_inputs):
            child = StepRun(
                run_id=run.id,
                step_key=foreach_child_key(parent.step_key, index),
                task_type=parent.task_type,
                input_data=child_input,
                depends_on=list(parent.depends_on or []),
                spec_json=child_spec,
                parent_step_id=parent.id,
                foreach_index=index,
                retry_limit=parent.retry_limit,
                backoff_seconds=parent.backoff_seconds,
                backoff_multiplier=parent.backoff_multiplier,
                required=parent.required and spec.get("partial_failure", "fail_fast") != "continue",
                timeout_seconds=parent.timeout_seconds,
                status="pending",
                available_at=now,
            )
            db.add(child)
            expanded_count += 1
        event_service.emit(
            db, "step.fanout_created", run_id=run.id, step=parent,
            message=f"Created {len(items)} fan-out task(s)", payload={"children": len(items)},
        )
        changed = True
    if changed:
        db.flush()
    return changed


def _settle_fanout_parents(db: Session, run: WorkflowRun, steps: list[StepRun], now: datetime) -> bool:
    children_by_parent: dict[str, list[StepRun]] = {}
    for step in steps:
        if step.parent_step_id:
            children_by_parent.setdefault(step.parent_step_id, []).append(step)
    changed = False
    by_id = {step.id: step for step in steps}
    for parent_id, children in children_by_parent.items():
        parent = by_id.get(parent_id)
        if parent is None or parent.status != "waiting_children":
            continue
        if any(child.status not in TERMINAL_STEP_STATUSES for child in children):
            continue
        ordered = sorted(children, key=lambda item: item.foreach_index if item.foreach_index is not None else -1)
        errors = [child for child in ordered if child.status in {"failed", "cancelled", "skipped"}]
        policy = (parent.spec_json or {}).get("partial_failure", "fail_fast")
        if run.cancel_requested:
            parent.status = "cancelled"
        elif errors and policy == "fail_fast":
            parent.status = "failed"
            parent.error = {
                "code": "foreach_child_failed",
                "message": f"{len(errors)} fan-out task(s) failed",
                "children": [child.step_key for child in errors],
            }
        else:
            parent.status = "succeeded"
            parent.output_data = [
                child.output_data if child.status == "succeeded" else {"error": child.error, "status": child.status}
                for child in ordered
            ]
        parent.finished_at = now
        parent.updated_at = now
        event_service.emit(
            db, f"step.{parent.status}", run_id=run.id, step=parent,
            level="error" if parent.status == "failed" else "info",
            message=f"Fan-out {parent.status}", payload={"children": len(children), "failed": len(errors)},
        )
        changed = True
    return changed


def _settle_subworkflow_steps(db: Session, run: WorkflowRun, steps: list[StepRun], now: datetime) -> bool:
    changed = False
    for step in steps:
        if step.status != "waiting_subworkflow" or not step.child_run_id:
            continue
        child = db.get(WorkflowRun, step.child_run_id)
        if child is None:
            step.status = "failed"
            step.error = {"code": "subworkflow_missing", "message": "The child workflow run could not be found"}
            step.finished_at = now
            changed = True
            continue
        if run.cancel_requested and child.status not in TERMINAL_RUN_STATUSES and not child.cancel_requested:
            request_cancel(db, child, actor="parent-cancellation")
        if child.status not in TERMINAL_RUN_STATUSES:
            continue
        if child.status == "succeeded":
            step.status = "succeeded"
            step.output_data = child.output_data
        elif child.status == "cancelled":
            step.status = "cancelled"
            step.error = {"code": "subworkflow_cancelled", "message": "The child workflow run was cancelled"}
        else:
            step.status = "failed"
            step.error = {"code": "subworkflow_failed", "message": "The child workflow run failed", "child_run_id": child.id}
        step.finished_at = now
        step.updated_at = now
        event_service.emit(
            db, f"step.{step.status}", run_id=run.id, step=step,
            level="error" if step.status == "failed" else "info",
            message=f"Sub-workflow {step.status}", payload={"child_run_id": child.id, "child_status": child.status},
        )
        changed = True
    return changed


def _settle_compensations(db: Session, run: WorkflowRun, steps: list[StepRun], now: datetime) -> bool:
    compensation_steps = [step for step in steps if (step.spec_json or {}).get("compensation_only")]
    if not compensation_steps:
        return False
    if any(step.status != "waiting_compensation" for step in compensation_steps):
        return False
    main_steps = [
        step for step in steps
        if not (step.spec_json or {}).get("compensation_only")
        and not (step.spec_json or {}).get("failure_handler")
    ]
    if any(step.status not in TERMINAL_STEP_STATUSES for step in main_steps):
        return False
    has_failure = bool(run.error and run.error.get("code") == "run_timeout") or any(
        step.required
        and (step.status == "failed" or (step.status == "skipped" and (step.error or {}).get("code") != "condition_false"))
        for step in main_steps
    )
    if run.cancel_requested and not (run.error and run.error.get("code") == "run_timeout"):
        return False
    if not has_failure:
        for step in compensation_steps:
            step.status = "skipped"
            step.required = False
            step.error = {"code": "compensation_not_required", "message": "No rollback was needed"}
            step.finished_at = now
            step.updated_at = now
        event_service.emit(db, "run.compensation_skipped", run_id=run.id, message="No compensation was needed")
        return True

    by_key = {step.step_key: step for step in steps}
    compensation_by_key = {step.step_key: step for step in compensation_steps}
    successful = sorted(
        (step for step in main_steps if step.status == "succeeded" and (step.spec_json or {}).get("compensate")),
        key=lambda step: ensure_utc(step.finished_at) or ensure_utc(step.created_at) or ensure_utc(now),
        reverse=True,
    )
    selected: list[tuple[StepRun, StepRun]] = []
    seen: set[str] = set()
    for action in successful:
        comp_key = (action.spec_json or {}).get("compensate")
        comp = compensation_by_key.get(comp_key)
        if comp is not None and comp_key not in seen:
            selected.append((comp, action))
            seen.add(comp_key)
    previous_key: str | None = None
    for comp, action in selected:
        deps = list(comp.depends_on or [])
        if action.step_key not in deps:
            deps.append(action.step_key)
        if previous_key and previous_key not in deps:
            deps.append(previous_key)
        comp.depends_on = deps
        policy = dict(comp.spec_json or {})
        policy["join"] = "all_done"
        policy["compensation_active"] = True
        comp.spec_json = policy
        comp.status = "pending"
        comp.available_at = now
        comp.updated_at = now
        comp.required = False
        previous_key = comp.step_key
    for comp in compensation_steps:
        if comp.step_key in seen:
            continue
        comp.status = "skipped"
        comp.required = False
        comp.error = {"code": "compensation_not_required", "message": "No successful action required this compensation"}
        comp.finished_at = now
        comp.updated_at = now
    event_service.emit(
        db, "run.compensation_started", run_id=run.id,
        message=f"Started {len(selected)} compensation step(s)",
        payload={"steps": [step.step_key for step, _ in selected]},
    )
    return True


def _start_failure_handlers(db: Session, run: WorkflowRun, steps: list[StepRun], now: datetime) -> bool:
    failure_specs = (run.definition_json or {}).get("on_failure", [])
    if not failure_specs or any((step.spec_json or {}).get("failure_handler") for step in steps):
        return False
    main_steps = [
        step for step in steps
        if not (step.spec_json or {}).get("failure_handler")
        and not (step.spec_json or {}).get("compensation_only")
    ]
    if any(step.status not in TERMINAL_STEP_STATUSES for step in main_steps):
        return False
    if any(
        (step.spec_json or {}).get("compensation_only") and step.status not in TERMINAL_STEP_STATUSES
        for step in steps
    ):
        return False
    has_failure = bool(run.error and run.error.get("code") == "run_timeout") or any(
        step.required
        and (step.status == "failed" or (step.status == "skipped" and (step.error or {}).get("code") != "condition_false"))
        for step in main_steps
    )
    if not has_failure or (run.cancel_requested and not (run.error and run.error.get("code") == "run_timeout")):
        return False

    for spec in failure_specs:
        handler_spec = {key: value for key, value in spec.items() if key != "input"}
        handler_spec["failure_handler"] = True
        handler_spec["join"] = spec.get("join", "all_done")
        db.add(
            StepRun(
                run_id=run.id,
                step_key=spec["id"],
                task_type=spec["type"],
                input_data=spec.get("input", {}),
                depends_on=list(spec.get("depends_on", [])),
                spec_json=handler_spec,
                retry_limit=int(spec.get("retries", 0)),
                backoff_seconds=int(spec.get("backoff_seconds", 2)),
                backoff_multiplier=float(spec.get("backoff_multiplier", 2.0)),
                required=False,
                timeout_seconds=int(spec.get("timeout_seconds", 300)),
                status="pending",
                available_at=now,
            )
        )
    event_service.emit(
        db, "run.failure_handlers_started", run_id=run.id,
        message=f"Started {len(failure_specs)} failure handler step(s)",
        payload={"steps": [spec["id"] for spec in failure_specs]},
    )
    db.flush()
    return True


def _start_subworkflow(
    db: Session,
    run: WorkflowRun,
    step: StepRun,
    *,
    workflow_input: dict[str, Any],
    step_outputs: dict[str, Any],
    now: datetime,
) -> bool:
    from app.models.workflow import Workflow

    resolved = resolve_templates(step.input_data or {}, workflow_input=workflow_input, step_outputs=step_outputs)
    if not isinstance(resolved, dict):
        raise Invalid("workflow.run input must resolve to an object", code="subworkflow_input_invalid")
    reference = resolved.get("workflow")
    if not isinstance(reference, str) or not reference.strip():
        raise Invalid("workflow.run input.workflow must identify a published workflow", code="subworkflow_target_invalid")
    parent_workflow = db.get(Workflow, run.workflow_id)
    if parent_workflow is None:
        raise NotFound("Parent workflow not found", code="workflow_not_found")
    if reference == reference.strip() and len(reference) == 32:
        targets = list(db.scalars(select(Workflow).where(Workflow.owner_id == parent_workflow.owner_id, Workflow.id == reference)).all())
    else:
        targets = list(
            db.scalars(
                select(Workflow).where(
                    Workflow.owner_id == parent_workflow.owner_id,
                    func.lower(Workflow.name) == reference.strip().lower(),
                ).limit(2)
            ).all()
        )
    if not targets:
        raise NotFound("Published child workflow not found", code="subworkflow_not_found")
    if len(targets) > 1:
        raise Invalid("A workflow name is ambiguous; use its workflow id", code="subworkflow_ambiguous")
    target = targets[0]
    depth = run.nesting_depth + 1
    if depth > settings.max_subworkflow_depth:
        raise Invalid("Maximum sub-workflow nesting depth exceeded", code="subworkflow_depth_exceeded")
    cursor: WorkflowRun | None = run
    while cursor is not None:
        if cursor.workflow_id == target.id:
            raise Invalid("Sub-workflow cycle detected", code="subworkflow_cycle")
        cursor = db.get(WorkflowRun, cursor.parent_run_id) if cursor.parent_run_id else None
    child_input = resolved.get("input", {})
    if not isinstance(child_input, dict):
        raise Invalid("workflow.run input.input must resolve to an object", code="subworkflow_input_invalid")
    version = resolved.get("version")
    if version is not None and (not isinstance(version, int) or isinstance(version, bool)):
        raise Invalid("workflow.run input.version must be an integer", code="subworkflow_version_invalid")
    generation = int((step.spec_json or {}).get("child_generation", 0))
    child, _ = create_run(
        db,
        target,
        version=version,
        input_data=child_input,
        idempotency_key=f"subworkflow:{run.id}:{step.id}:{generation}",
        trigger="subworkflow",
        triggered_by=run.triggered_by,
        priority=run.priority,
        queue_name=run.queue_name,
        parent_run_id=run.id,
        parent_step_run_id=step.id,
        nesting_depth=depth,
    )
    step.child_run_id = child.id
    step.started_at = step.started_at or now
    step.updated_at = now
    step.status = "waiting_subworkflow"
    if child.status in TERMINAL_RUN_STATUSES:
        _settle_subworkflow_steps(db, run, [step], now)
    event_service.emit(
        db, "subworkflow.started", run_id=run.id, step=step,
        message="Child workflow run started", payload={"child_run_id": child.id, "workflow_id": target.id, "version": child.version},
    )
    return True


def _fail_unresolvable_inputs(
    db: Session,
    run: WorkflowRun,
    steps: list[StepRun],
    by_key: dict[str, StepRun],
) -> bool:
    changed = False
    for step in steps:
        if step.status not in CLAIMABLE_STEP_STATUSES:
            continue
        if (step.spec_json or {}).get("foreach"):
            continue
        dependencies = [by_key[dep] for dep in step.depends_on if dep in by_key]
        if join_decision(dependencies, mode=(step.spec_json or {}).get("join", "all_success")) != "ready":
            continue
        outputs = {dep: by_key[dep].output_data for dep in step.depends_on if dep in by_key and by_key[dep].status == "succeeded"}
        try:
            resolve_templates(
                step.input_data or {},
                workflow_input=workflow_input_for_run(run),
                step_outputs=outputs,
            )
        except DataReferenceError as exc:
            now = utcnow()
            step.status = "failed"
            step.error = {
                "code": "input_reference_unavailable",
                "message": str(exc),
                "retryable": False,
            }
            step.finished_at = now
            step.updated_at = now
            event_service.emit(
                db,
                EventType.STEP_FAILED,
                run_id=run.id,
                step=step,
                level="error",
                message="Step input could not be resolved",
                payload={"code": "input_reference_unavailable", "retryable": False},
            )
            changed = True
    return changed


def _skip_doomed(db: Session, run: WorkflowRun, steps: list[StepRun], by_key: dict[str, StepRun]) -> bool:
    """Mark steps skipped when a dependency can no longer succeed."""
    changed = False
    progressed = True
    while progressed:
        progressed = False
        for step in steps:
            if step.status not in CLAIMABLE_STEP_STATUSES:
                continue
            dependencies = [by_key[dep] for dep in step.depends_on if dep in by_key]
            if join_decision(dependencies, mode=(step.spec_json or {}).get("join", "all_success")) != "skip":
                continue
            blocked_by = [dep for dep in step.depends_on if dep in by_key and by_key[dep].status in {"failed", "cancelled", "skipped"}]
            step.status = "skipped"
            step.updated_at = utcnow()
            step.error = {"code": "dependency_failed", "message": f"Dependency '{blocked_by[0]}' did not succeed", "dependencies": blocked_by}
            event_service.emit(
                db, EventType.STEP_SKIPPED, run_id=run.id, step=step, level="warning",
                message=f"Skipped because {', '.join(blocked_by)} did not succeed",
                payload={"blocked_by": blocked_by},
            )
            changed = True
            progressed = True
    return changed


def _release_ready(db: Session, run: WorkflowRun, steps: list[StepRun], by_key: dict[str, StepRun]) -> bool:
    """Dispatch steps whose dependencies succeeded, within the concurrency budget."""
    if run.status == "paused":
        return False
    budget = max(1, run.max_parallel or settings.dispatch_batch_size)
    in_flight = sum(1 for step in steps if step.status == "running")
    free = budget - in_flight
    if free <= 0:
        return False
    now = datetime.now(timezone.utc)
    ready: list[StepRun] = []
    changed = False
    for step in steps:
        if step.status not in CLAIMABLE_STEP_STATUSES:
            continue
        if run.cancel_requested and not (
            (step.spec_json or {}).get("failure_handler")
            and run.error
            and run.error.get("code") == "run_timeout"
        ):
            continue
        if step.enqueued_attempt == step.attempts:
            continue
        if step.lease_token:
            continue
        available_at = ensure_utc(step.available_at)
        if available_at is not None and available_at > now:
            continue
        dependencies = [by_key[dep] for dep in step.depends_on if dep in by_key]
        decision = join_decision(dependencies, mode=(step.spec_json or {}).get("join", "all_success"))
        if decision != "ready":
            continue
        if (step.spec_json or {}).get("foreach"):
            continue
        if step.task_type == "workflow.run":
            outputs = {dep: by_key[dep].output_data for dep in step.depends_on if dep in by_key and by_key[dep].status == "succeeded"}
            try:
                _start_subworkflow(
                    db, run, step, workflow_input=workflow_input_for_run(run), step_outputs=outputs,
                    now=now.replace(tzinfo=None),
                )
            except Exception as exc:
                step.status = "failed"
                step.error = {"code": getattr(exc, "code", "subworkflow_start_failed"), "message": str(exc), "retryable": False}
                step.finished_at = now.replace(tzinfo=None)
                step.updated_at = now.replace(tzinfo=None)
                event_service.emit(
                    db, EventType.STEP_FAILED, run_id=run.id, step=step, level="error",
                    message="Child workflow could not be started", payload={"code": step.error["code"]},
                )
            changed = True
            continue
        cache = (step.spec_json or {}).get("cache")
        if (
            cache
            and step.task_type != "approval"
            and not (step.spec_json or {}).get("has_secrets")
            and not _input_contains_secret_references(run.input_data)
        ):
            outputs = {dep: by_key[dep].output_data for dep in step.depends_on if dep in by_key and by_key[dep].status == "succeeded"}
            try:
                resolved_input = resolve_templates(step.input_data or {}, workflow_input=workflow_input_for_run(run), step_outputs=outputs)
            except DataReferenceError:
                resolved_input = None
            if resolved_input is not None:
                key = cache_key_material(step.task_type, resolved_input, cache.get("key", "input-hash"))
                cached = db.scalar(
                    select(StepCacheEntry).where(
                        StepCacheEntry.workflow_id == run.workflow_id,
                        StepCacheEntry.cache_key == key,
                        StepCacheEntry.expires_at > ensure_utc(now),
                    )
                )
                if cached is not None:
                    step.status = "succeeded"
                    step.output_data = cached.output_data
                    step.started_at = step.started_at or now.replace(tzinfo=None)
                    step.finished_at = now.replace(tzinfo=None)
                    step.updated_at = now.replace(tzinfo=None)
                    event_service.emit(
                        db, "step.cache_hit", run_id=run.id, step=step,
                        message="Reused a cached result",
                        payload={"expires_at": ensure_utc(cached.expires_at).isoformat()},
                    )
                    changed = True
                    continue
        if step.task_type == "approval":
            step.status = "waiting_approval"
            step.started_at = step.started_at or now.replace(tzinfo=None)
            step.deadline_at = step.started_at + timedelta(seconds=step.timeout_seconds)
            event_service.emit(db, "approval.required", run_id=run.id, step=step, message="Approval is required", payload={"approvers": (step.spec_json or {}).get("approvers", [])})
            # Outbound notification for approval-needed. Test runs stay silent.
            if not run.is_test:
                try:
                    from app.services import notification_service

                    workflow = db.get(Workflow, run.workflow_id)
                    if workflow is not None:
                        notification_service.dispatch_event(
                            db,
                            event="run.waiting_approval",
                            owner_id=workflow.owner_id,
                            workflow_id=run.workflow_id,
                            payload={"run_id": run.id, "workflow_id": run.workflow_id, "step_key": step.step_key},
                        )
                except Exception:  # noqa: BLE001
                    logger.warning("Approval notification failed for run %s", run.id, exc_info=True)
            changed = True
            continue
        if step.parent_step_id:
            fanout_limit = (step.spec_json or {}).get("max_concurrency")
            if fanout_limit:
                family_active = sum(
                    1 for item in steps
                    if item.parent_step_id == step.parent_step_id and item.status == "running"
                ) + sum(1 for item in ready if item.parent_step_id == step.parent_step_id)
                if family_active >= int(fanout_limit):
                    continue
        ready.append(step)
    if not ready:
        return changed
    dispatched = 0
    for step in ready[:free]:
        enqueue(
            db,
            TOPIC_TASK_READY,
            {
                "run_id": run.id,
                "step_run_id": step.id,
                "step_key": step.step_key,
                "task_type": step.task_type,
                "workflow_id": run.workflow_id,
                "workflow_version": run.version,
            },
        )
        step.enqueued_attempt = step.attempts
        event_service.emit(
            db, EventType.STEP_READY, run_id=run.id, step=step,
            message=f"Ready for a worker (attempt {step.attempts + 1})",
            payload={"task_type": step.task_type, "dependencies": step.depends_on},
        )
        dispatched += 1
    return changed or dispatched > 0


def _settle_approval_timeouts(db: Session, run: WorkflowRun, steps: list[StepRun], now: datetime) -> bool:
    changed = False
    for step in steps:
        if step.status != "waiting_approval" or not step.deadline_at:
            continue
        deadline = ensure_utc(step.deadline_at)
        if deadline is None or deadline > ensure_utc(now):
            continue
        behavior = (step.spec_json or {}).get("on_timeout", "fail")
        step.finished_at = now
        step.deadline_at = None
        if behavior == "approve":
            step.status = "succeeded"
            step.output_data = {"approved": True, "actor": "timeout", "comment": "Automatically approved on timeout"}
        elif behavior == "reject":
            step.status = "failed"
            step.error = {"code": "approval_timed_out", "message": "Approval timed out and was rejected"}
        else:
            step.status = "failed"
            step.error = {"code": "approval_timed_out", "message": "Approval timed out"}
        event_service.emit(db, f"approval.{behavior}", run_id=run.id, step=step, level="warning", message=f"Approval timed out ({behavior})")
        changed = True
    return changed


def _finish_run(db: Session, run: WorkflowRun, status: str) -> None:
    if run.status == status and run.finished_at is not None:
        return
    now = utcnow()
    run.status = status
    run.finished_at = now
    run.started_at = run.started_at or now
    run.updated_at = now
    steps = steps_for(db, run.id)
    run.output_data = {
        step.step_key: step.output_data
        for step in steps
        if step.parent_step_id is None and step.status == "succeeded"
    }
    failed = [step for step in steps if step.status == "failed"]
    blocked_required = [
        step for step in steps
        if step.required and step.status == "skipped" and (step.error or {}).get("code") != "condition_false"
    ]
    if status == "failed":
        run.error = {
            "code": "step_failed",
            "message": (
                f"{len(blocked_required)} required step(s) were skipped"
                if blocked_required and not any(step.required for step in failed)
                else f"{sum(1 for step in failed if step.required)} required step(s) failed"
                + (f"; {len(blocked_required)} required step(s) were skipped" if blocked_required else "")
            ),
            "steps": [
                {"key": step.step_key, "status": step.status, "error": step.error}
                for step in [*failed, *blocked_required][:10]
            ],
        }
    duration = duration_seconds(run.started_at, now) or 0.0
    event_service.emit(
        db,
        f"run.{status}",
        run_id=run.id,
        level="error" if status == "failed" else "info",
        message={
            "succeeded": "Run completed successfully",
            "failed": "Run finished with failures",
            "cancelled": "Run was cancelled",
        }.get(status, f"Run {status}"),
        payload={
            "duration_seconds": round(duration, 3),
            "step_counts": _count_by_status(steps),
            "failed_steps": [step.step_key for step in failed],
            "blocked_required_steps": [step.step_key for step in blocked_required],
        },
    )
    if status == "succeeded" and not run.is_test:
        from app.services.trigger_service import fire_workflow_success_triggers

        fire_workflow_success_triggers(db, run)

    # Outbound notifications (Phase 6). Test runs never notify. Failures are
    # logged, never raised.
    if run.is_test:
        return
    try:
        from app.services import notification_service

        workflow = db.get(Workflow, run.workflow_id)
        if workflow is not None:
            notification_service.dispatch_event(
                db,
                event=f"run.{status}",
                owner_id=workflow.owner_id,
                workflow_id=run.workflow_id,
                payload={
                    "run_id": run.id,
                    "workflow_id": run.workflow_id,
                    "status": status,
                    "duration_seconds": round(duration, 3),
                },
            )
    except Exception:  # noqa: BLE001
        logger.warning("Notification dispatch failed for run %s", run.id, exc_info=True)


def _count_by_status(steps: Iterable[StepRun]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for step in steps:
        counts[step.status] = counts.get(step.status, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# Cancellation and retry
# --------------------------------------------------------------------------- #
def request_cancel(db: Session, run: WorkflowRun, *, actor: str | None = None) -> WorkflowRun:
    if run.status in TERMINAL_RUN_STATUSES:
        return run
    if run.cancel_requested:
        settle_run(db, run)
        return run
    run.cancel_requested = True
    run.cancel_requested_at = utcnow()
    event_service.emit(
        db, EventType.RUN_CANCELLATION_REQUESTED, run_id=run.id,
        message="Cancellation requested; running steps are asked to stop",
        payload={}, actor=actor,
    )
    settle_run(db, run)
    db.flush()
    return run


def settle_due_runs(db: Session, *, limit: int = 200) -> int:
    """Refresh active runs so run/SLA/approval deadlines fire without a worker callback."""
    rows = list(
        db.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.status.in_(("queued", "running", "cancelling")))
            .order_by(WorkflowRun.created_at)
            .limit(limit)
        ).all()
    )
    for run in rows:
        settle_run(db, run)
    return len(rows)


def store_step_cache_result(db: Session, run: WorkflowRun, step: StepRun) -> None:
    spec = step.spec_json or {}
    policy = spec.get("cache")
    if not policy or spec.get("has_secrets") or _input_contains_secret_references(run.input_data):
        return
    if _contains_artifact_pointer(step.output_data):
        # Artifact URIs are scoped to the producing run and are not reusable
        # cache values for a different run.
        return
    siblings = step_map(steps_for(db, run.id))
    outputs = {
        key: siblings[key].output_data
        for key in (step.depends_on or [])
        if key in siblings and siblings[key].status == "succeeded"
    }
    resolved = resolve_templates(step.input_data or {}, workflow_input=workflow_input_for_run(run), step_outputs=outputs)
    key = cache_key_material(step.task_type, resolved, policy.get("key", "input-hash"))
    expires = utcnow() + timedelta(seconds=int(policy["ttl_seconds"]))
    existing = db.scalar(
        select(StepCacheEntry).where(
            StepCacheEntry.workflow_id == run.workflow_id,
            StepCacheEntry.cache_key == key,
        )
    )
    if existing is not None:
        existing.output_data = step.output_data
        existing.expires_at = expires
        return
    try:
        with db.begin_nested():
            db.add(
                StepCacheEntry(
                    workflow_id=run.workflow_id,
                    cache_key=key,
                    output_data=step.output_data,
                    expires_at=expires,
                )
            )
            db.flush()
    except IntegrityError:
        # A concurrent completion filled the same cache slot. Refresh that row
        # instead of failing a successfully completed task.
        existing = db.scalar(
            select(StepCacheEntry).where(
                StepCacheEntry.workflow_id == run.workflow_id,
                StepCacheEntry.cache_key == key,
            )
        )
        if existing is not None:
            existing.output_data = step.output_data
            existing.expires_at = expires


def _contains_artifact_pointer(value: Any) -> bool:
    if isinstance(value, dict):
        return "artifact_id" in value or any(_contains_artifact_pointer(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_artifact_pointer(item) for item in value)
    return False


def pause_run(db: Session, run: WorkflowRun, *, actor: str | None = None) -> WorkflowRun:
    if run.status in TERMINAL_RUN_STATUSES:
        raise Conflict("A finished run cannot be paused", code="run_not_active")
    if run.status == "paused":
        return run
    now = utcnow()
    run.status = "paused"
    run.paused_at = now
    run.updated_at = now
    event_service.emit(db, "run.paused", run_id=run.id, message="Run paused", payload={}, actor=actor)
    db.flush()
    return run


def resume_run(db: Session, run: WorkflowRun, *, actor: str | None = None) -> WorkflowRun:
    if run.status != "paused":
        raise Conflict("This run is not paused", code="run_not_paused")
    now = utcnow()
    steps = steps_for(db, run.id)
    run.status = "running" if any(step.status == "running" for step in steps) else "queued"
    run.paused_at = None
    run.updated_at = now
    event_service.emit(db, "run.resumed", run_id=run.id, message="Run resumed", payload={}, actor=actor)
    settle_run(db, run)
    db.flush()
    return run


def decide_approval(
    db: Session,
    run: WorkflowRun,
    *,
    step_key: str,
    actor: str,
    decision: str,
    comment: str = "",
    is_admin: bool = False,
) -> StepRun:
    step = db.scalar(select(StepRun).where(StepRun.run_id == run.id, StepRun.step_key == step_key))
    if step is None or step.task_type != "approval":
        raise NotFound("Approval step not found", code="approval_not_found")
    if step.status != "waiting_approval":
        raise Conflict("This approval is no longer waiting for a decision", code="approval_not_pending")
    allowed = (step.spec_json or {}).get("approvers", [])
    if allowed and actor not in allowed and not is_admin:
        from app.core.errors import Forbidden

        raise Forbidden("You are not an approver for this step", code="approval_forbidden")
    now = utcnow()
    approved = decision == "approve"
    step.status = "succeeded" if approved else "failed"
    step.output_data = {"approved": approved, "actor": actor, "comment": comment} if approved else None
    step.error = None if approved else {"code": "approval_rejected", "message": comment or "Approval was rejected", "actor": actor}
    step.finished_at = now
    step.deadline_at = None
    step.updated_at = now
    event_service.emit(
        db,
        "approval.approved" if approved else "approval.rejected",
        run_id=run.id,
        step=step,
        level="info" if approved else "warning",
        message=f"Approval {decision} by {actor}",
        payload={"decision": decision, "comment": comment},
        actor=actor,
    )
    settle_run(db, run)
    db.flush()
    return step


def retryable_steps(steps: Iterable[StepRun]) -> list[StepRun]:
    """Steps a user may reasonably retry: terminal failures in the current run."""
    return [step for step in steps if step.status == "failed"]


def retry_failed_steps(
    db: Session,
    run: WorkflowRun,
    *,
    step_keys: list[str] | None = None,
    reset_downstream: bool = True,
    input_data: dict[str, Any] | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Reset failed steps (and optionally their descendants) and re-dispatch.

    Only steps that are *terminal and failed* can be retried; a step that is
    currently running is left alone.
    """
    if run.status not in TERMINAL_RUN_STATUSES:
        raise Conflict("This run has not finished yet", code="run_not_finished")
    steps = steps_for(db, run.id)
    by_key = step_map(steps)
    candidates = [step for step in steps if step.status in {"failed", "skipped"}]
    if step_keys:
        missing = [key for key in step_keys if key not in by_key]
        if missing:
            raise NotFound(f"Step(s) not found in this run: {', '.join(missing)}", code="step_not_found")
        candidates = [by_key[key] for key in step_keys]
        candidates = [step for step in candidates if step.status in {"failed", "skipped", "succeeded", "cancelled"}]
    if not candidates:
        raise Conflict("There is nothing to retry in this run", code="nothing_to_retry")

    edges = {step.step_key: list(step.depends_on) for step in steps}
    reset_keys = {step.step_key for step in candidates}
    if reset_downstream:
        reset_keys |= dag.downstream_of(edges, reset_keys)
    # Fan-out children are runtime rows and are not ordinary graph edges. Reset
    # the parent and its complete child family together for a deterministic rerun.
    by_id = {step.id: step for step in steps}
    for key in list(reset_keys):
        step = by_key.get(key)
        if step is None:
            continue
        if step.parent_step_id and step.parent_step_id in by_id:
            reset_keys.add(by_id[step.parent_step_id].step_key)
        reset_keys.update(child.step_key for child in steps if child.parent_step_id == step.id)

    now = utcnow()
    reset: list[str] = []
    for key in sorted(reset_keys):
        step = by_key[key]
        if step.status == "running":
            continue
        step.status = "pending"
        step.attempts = 0
        step.enqueued_attempt = None
        step.output_data = None
        step.error = None
        step.lease_token = None
        step.lease_expires_at = None
        step.deadline_at = None
        step.worker_id = None
        step.available_at = now
        step.started_at = None
        step.finished_at = None
        step.updated_at = now
        if step.task_type == "workflow.run":
            spec = dict(step.spec_json or {})
            spec["child_generation"] = int(spec.get("child_generation", 0)) + 1
            step.spec_json = spec
            step.child_run_id = None
        if (step.spec_json or {}).get("foreach"):
            step.spec_json = {key: value for key, value in step.spec_json.items() if key != "foreach_expanded"}
        for attempt in list(step.attempts_history):
            db.delete(attempt)
        reset.append(key)

    if input_data is not None:
        check_run_input(input_data)
        run.input_data = input_data
    run.cancel_requested = False
    run.cancel_requested_at = None
    run.status = "queued"
    run.finished_at = None
    run.error = None
    run.paused_at = None
    timeout_seconds = (run.definition_json or {}).get("timeout_seconds")
    sla_seconds = (run.definition_json or {}).get("sla_seconds")
    run.deadline_at = now + timedelta(seconds=int(timeout_seconds)) if timeout_seconds else None
    run.sla_deadline_at = now + timedelta(seconds=int(sla_seconds)) if sla_seconds else None
    run.sla_breached_at = None
    run.updated_at = now
    event_service.emit(
        db, EventType.RUN_RETRY_REQUESTED, run_id=run.id,
        message=f"Retrying {len(candidates)} failed step(s)" + (f" and {len(reset) - len(candidates)} downstream step(s)" if reset_downstream else ""),
        payload={"retried_steps": sorted(step.step_key for step in candidates), "reset_steps": reset},
        actor=actor,
    )
    settle_run(db, run)
    db.flush()
    return {"retried_steps": sorted(step.step_key for step in candidates), "reset_steps": reset}


# --------------------------------------------------------------------------- #
# Retry backoff
# --------------------------------------------------------------------------- #
def retry_delay_seconds(step: StepRun) -> float:
    """Exponential backoff with bounded jitter."""
    base = max(0, step.backoff_seconds)
    multiplier = max(1.0, step.backoff_multiplier)
    exponent = max(0, step.attempts - 1)
    raw = base * (multiplier ** exponent)
    step_policy = step.spec_json or {}
    max_delay = step_policy.get("retry_max_delay_seconds")
    capped = min(float(max_delay or settings.retry_max_delay_seconds), raw)
    jitter_ratio = float(step_policy.get("retry_jitter") if step_policy.get("retry_jitter") is not None else settings.retry_jitter)
    if jitter_ratio > 0 and capped > 0:
        jitter = capped * jitter_ratio
        capped = max(0.0, capped + random.uniform(-jitter, jitter))
    return round(capped, 3)


def idempotency_key_for(run_id: str, step_key: str, attempt: int) -> str:
    digest = hashlib.sha256(f"{run_id}:{step_key}:{attempt}".encode("utf-8")).hexdigest()
    return f"{run_id[:8]}-{step_key}-a{attempt}-{digest[:12]}"


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
def duration_seconds(started_at: datetime | None, finished_at: datetime | None) -> float | None:
    if started_at is None:
        return None
    start = ensure_utc(started_at)
    end = ensure_utc(finished_at) if finished_at is not None else datetime.now(timezone.utc)
    if start is None or end is None:
        return None
    return round(max(0.0, (end - start).total_seconds()), 3)


def progress(steps: Iterable[StepRun]) -> float:
    items = list(steps)
    if not items:
        return 0.0
    done = sum(1 for step in items if step.status in TERMINAL_STEP_STATUSES)
    return round(done / len(items), 4)


def wait_reason_for(db: Session, run: WorkflowRun, step: StepRun, by_key: dict[str, StepRun]) -> str | None:
    """Why a non-terminal step is not executing yet (additive UI hint).

    Mirrors the claim path's skip order in ``worker_service.claim_task`` without
    mutating anything: paused runs, approvals, unmet dependencies, open circuit
    breakers, exhausted concurrency budgets and empty rate buckets are reported
    distinctly; anything else that is ready is ``waiting_for_worker``.
    """
    if step.status in TERMINAL_STEP_STATUSES or step.status == "running":
        return None
    if step.status == "waiting_approval":
        return "approval_pending"
    if run.status == "paused" or run.paused_at is not None:
        return "paused"
    if step.status not in CLAIMABLE_STEP_STATUSES | {"queued", "scheduled"}:
        return None
    now = utcnow()
    available_at = ensure_utc(step.available_at)
    now_utc = ensure_utc(now)
    if step.status == "retrying" and available_at is not None and now_utc is not None and available_at > now_utc:
        # Backoff timer has not elapsed; the UI already shows ``retry_at``.
        return None
    siblings = [by_key[key] for key in (step.depends_on or []) if key in by_key]
    if join_decision(siblings, mode=(step.spec_json or {}).get("join", "all_success")) != "ready":
        return "waiting_for_dependencies"
    # Circuit breaker (read-only mirror of worker_service._circuit_breaker_open).
    if _circuit_breaker_open_for(db, run, step, now):
        return "circuit_open"
    if _concurrency_exhausted(db, run, step, by_key):
        return "concurrency_limited"
    if _rate_limited(db, run, step, now):
        return "rate_limited"
    return "waiting_for_worker"


def _circuit_breaker_open_for(db: Session, run: WorkflowRun, step: StepRun, now: datetime) -> bool:
    from app.models.workflow import Workflow as WorkflowModel

    from app.services import worker_service

    workflow = db.get(WorkflowModel, run.workflow_id)
    if workflow is None:
        return False
    return worker_service._circuit_breaker_open(db, workflow_owner_id=workflow.owner_id, step=step, now=now)


def _concurrency_exhausted(db: Session, run: WorkflowRun, step: StepRun, by_key: dict[str, StepRun]) -> bool:
    # Run-level budget mirrors claim_task.
    budget = max(1, run.max_parallel or 1)
    running_in_run = sum(1 for item in by_key.values() if item.status == "running")
    if running_in_run >= budget:
        return True
    # Fan-out sibling cap.
    if step.parent_step_id:
        fanout_limit = (step.spec_json or {}).get("max_concurrency")
        if fanout_limit:
            active_siblings = sum(
                1
                for item in by_key.values()
                if item.parent_step_id == step.parent_step_id and item.status == "running"
            )
            if active_siblings >= int(fanout_limit):
                return True
    # Global / queue / owner / resource gates, read-only.
    from app.models.run import ConcurrencyGate
    from app.services import worker_service

    limits: list[tuple[str, str | None, str | None, str | None, int]] = []
    if settings.max_global_running_tasks > 0:
        limits.append(("global", None, None, None, settings.max_global_running_tasks))
    queue_limit = int(settings.queue_concurrency_limits.get(step.queue_name, 0) or 0)
    if queue_limit > 0:
        limits.append(("queue", None, step.queue_name, None, queue_limit))
    if settings.max_running_tasks_per_owner > 0:
        from app.models.workflow import Workflow as WorkflowModel

        workflow = db.get(WorkflowModel, run.workflow_id)
        if workflow is not None:
            limits.append(("owner", workflow.owner_id, None, None, settings.max_running_tasks_per_owner))
    if step.concurrency_key and step.concurrency_limit:
        from app.models.workflow import Workflow as WorkflowModel

        workflow = db.get(WorkflowModel, run.workflow_id)
        if workflow is not None:
            limits.append(("resource", workflow.owner_id, None, step.concurrency_key, step.concurrency_limit))
    for scope, owner_id, queue_name, resource_key, capacity in limits:
        gate = ConcurrencyGate(
            scope=scope, owner_id=owner_id, queue_name=queue_name, resource_key=resource_key, capacity=capacity
        )
        if worker_service._active_count_for_gate(db, gate) >= capacity:
            return True
    return False


def _rate_limited(db: Session, run: WorkflowRun, step: StepRun, now: datetime) -> bool:
    from app.models.run import TaskRateBucket
    from app.models.workflow import Workflow as WorkflowModel
    from app.services import worker_service

    per_minute = worker_service._rate_limit_for(step)
    if per_minute <= 0:
        return False
    workflow = db.get(WorkflowModel, run.workflow_id)
    if workflow is None:
        return False
    policy = step.spec_json or {}
    bucket_name = str(policy.get("rate_limit_key") or step.task_type)
    key = hashlib.sha256(f"{workflow.owner_id}\0{bucket_name}".encode("utf-8")).hexdigest()
    bucket = db.get(TaskRateBucket, key)
    if bucket is None:
        return False
    bucket_updated = bucket.updated_at or now
    if bucket_updated.tzinfo is not None:
        bucket_updated = bucket_updated.replace(tzinfo=None)
    elapsed = max(0.0, (now - bucket_updated).total_seconds())
    tokens = min(float(per_minute), float(bucket.tokens) + elapsed * per_minute / 60.0)
    return tokens < 1.0


def step_view(step: StepRun, *, downstream: list[str] | None = None, wait_reason: str | None = None) -> dict[str, Any]:
    logs = step.logs or []
    last_log_at = logs[-1].get("at") if logs and isinstance(logs[-1], dict) else None
    return {
        "id": step.id,
        "key": step.step_key,
        "name": (step.spec_json or {}).get("name", ""),
        "type": step.task_type,
        "status": step.status,
        "priority": step.priority,
        "queue": step.queue_name,
        "depends_on": list(step.depends_on or []),
        "attempts": step.attempts,
        "retry_limit": step.retry_limit,
        "required": step.required,
        "timeout_seconds": step.timeout_seconds,
        "available_at": step.available_at,
        "deadline_at": step.deadline_at,
        "started_at": step.started_at,
        "finished_at": step.finished_at,
        "duration_seconds": duration_seconds(step.started_at, step.finished_at),
        "worker_id": step.worker_id,
        "output": step.output_data,
        "error": step.error,
        "input": step.input_data or {},
        "log_lines": len(logs),
        "last_log_at": last_log_at,
        "downstream": downstream or [],
        "retry_at": step.available_at if step.status == "retrying" else None,
        "spec": {key: value for key, value in (step.spec_json or {}).items() if key != "has_secrets"},
        "parent_step_id": step.parent_step_id,
        "child_run_id": step.child_run_id,
        "foreach_index": step.foreach_index,
        "wait_reason": wait_reason,
    }


def run_summary_view(db: Session, run: WorkflowRun, *, workflow_name: str = "", steps: list[StepRun] | None = None) -> dict[str, Any]:
    items = steps if steps is not None else steps_for(db, run.id)
    counts = _count_by_status(items)
    return {
        "id": run.id,
        "workflow_id": run.workflow_id,
        "parent_run_id": run.parent_run_id,
        "nesting_depth": run.nesting_depth,
        "logical_date": run.logical_date,
        "interval_start": run.interval_start,
        "interval_end": run.interval_end,
        "workflow_name": workflow_name,
        "version": run.version,
        "status": run.status,
        "trigger": run.trigger,
        "is_test": run.is_test,
        "priority": run.priority,
        "queue": run.queue_name,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_seconds": duration_seconds(run.started_at, run.finished_at),
        "step_counts": counts,
        "total_steps": len(items),
        "completed_steps": sum(1 for step in items if step.status in TERMINAL_STEP_STATUSES),
        "progress": progress(items),
        "cancel_requested": run.cancel_requested,
    }


def run_detail_view(db: Session, run: WorkflowRun, *, workflow_name: str = "") -> dict[str, Any]:
    steps = steps_for(db, run.id)
    edges = {step.step_key: list(step.depends_on or []) for step in steps}
    downstream: dict[str, list[str]] = {key: [] for key in edges}
    for key, parents in edges.items():
        for parent in parents:
            downstream.setdefault(parent, []).append(key)
    retryable = [step.step_key for step in steps if step.status == "failed"]
    return {
        "id": run.id,
        "workflow_id": run.workflow_id,
        "parent_run_id": run.parent_run_id,
        "nesting_depth": run.nesting_depth,
        "logical_date": run.logical_date,
        "interval_start": run.interval_start,
        "interval_end": run.interval_end,
        "workflow_name": workflow_name,
        "version": run.version,
        "status": run.status,
        "trigger": run.trigger,
        "is_test": run.is_test,
        "priority": run.priority,
        "queue": run.queue_name,
        "input": run.input_data or {},
        "output": run.output_data,
        "error": run.error,
        "cancel_requested": run.cancel_requested,
        "cancel_requested_at": run.cancel_requested_at,
        "max_parallel": run.max_parallel,
        "deadline_at": run.deadline_at,
        "sla_deadline_at": run.sla_deadline_at,
        "sla_breached_at": run.sla_breached_at,
        "paused_at": run.paused_at,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_seconds": duration_seconds(run.started_at, run.finished_at),
        "steps": [
            step_view(
                step,
                downstream=sorted(downstream.get(step.step_key, [])),
                wait_reason=wait_reason_for(db, run, step, {item.step_key: item for item in steps}),
            )
            for step in steps
        ],
        "step_counts": _count_by_status(steps),
        "progress": progress(steps),
        "latest_event_seq": event_service.latest_seq(db, run.id),
        "retryable_steps": retryable,
    }


def attempt_view(attempt: StepAttempt) -> dict[str, Any]:
    error = attempt.error or {}
    return {
        "id": attempt.id,
        "attempt": attempt.attempt_no,
        "worker_id": attempt.worker_id,
        "status": attempt.status,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
        "duration_seconds": duration_seconds(attempt.started_at, attempt.finished_at),
        "output": attempt.output_data,
        "error": attempt.error,
        "error_message": error.get("message") if isinstance(error, dict) else None,
        "error_class": "retryable" if error.get("retryable") else ("permanent" if error else None),
        "log_lines": len(attempt.logs or []),
    }


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "ACTIVE_STEP_STATUSES",
    "CLAIMABLE_STEP_STATUSES",
    "TERMINAL_RUN_STATUSES",
    "TERMINAL_STEP_STATUSES",
    "attempt_view",
    "create_run",
    "duration_seconds",
    "get_run",
    "idempotency_key_for",
    "list_runs",
    "ai_usage_for_run",
    "progress",
    "request_cancel",
    "retry_delay_seconds",
    "retry_failed_steps",
    "retryable_steps",
    "run_detail_view",
    "run_summary_view",
    "settle_run",
    "step_map",
    "step_view",
    "steps_for",
]