"""Alert rule evaluation and the workflow cost guard (Stage H, H4).

Rules are evaluated on run/step lifecycle events (called from the dispatch
loop) and on a periodic sweep for time-based conditions (no_successful_run,
cost budgets). Firing is deduplicated by per-rule cooldown.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.alert import AlertEvent, AlertRule, WorkflowBudget
from app.models.run import StepRun, WorkflowRun, utcnow


def _in_cooldown(rule: AlertRule, now: datetime) -> bool:
    if rule.last_fired_at is None:
        return False
    last = rule.last_fired_at
    # SQLite stores naive UTC; normalize both to naive for comparison.
    if last.tzinfo is not None:
        last = last.replace(tzinfo=None)
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return (now - last).total_seconds() < rule.cooldown_seconds


def _fire(
    db: Session,
    rule: AlertRule,
    message: str,
    details: dict[str, Any],
    workflow_id: str | None = None,
    run_id: str | None = None,
) -> AlertEvent | None:
    """Record an alert event unless the rule is in cooldown. Returns the event or None."""
    now = utcnow()
    if _in_cooldown(rule, now):
        return None
    rule.last_fired_at = now
    event = AlertEvent(
        rule_id=rule.id,
        user_id=rule.user_id,
        workflow_id=workflow_id or rule.workflow_id,
        run_id=run_id,
        condition=rule.condition,
        message=message,
        details=details,
    )
    db.add(event)
    db.flush()
    # Best-effort notification through the rule's channel.
    if rule.channel_id:
        try:
            from app.services import notification_service

            notification_service.dispatch_to_channel(
                db,
                rule.channel_id,
                event_type="alert.fired",
                payload={"rule": rule.name, "condition": rule.condition, "message": message, "details": details},
            )
        except Exception:  # noqa: BLE001 - alerts must never break the dispatch loop
            pass
    return event


def evaluate_step_event(db: Session, step: StepRun, run: WorkflowRun) -> list[AlertEvent]:
    """Evaluate step_failed / run_failed rules after a step reaches a terminal state."""
    fired: list[AlertEvent] = []
    rules = db.scalars(
        select(AlertRule).where(
            AlertRule.is_active.is_(True),
            (AlertRule.workflow_id.is_(None)) | (AlertRule.workflow_id == run.workflow_id),
            AlertRule.condition.in_(("step_failed", "run_failed")),
        )
    ).all()
    for rule in rules:
        params = rule.params or {}
        if rule.condition == "step_failed":
            if step.status != "failed":
                continue
            wanted = params.get("step_key")
            if wanted and wanted != step.step_key:
                continue
            consecutive = int(params.get("consecutive", 1))
            if consecutive > 1:
                # Count consecutive failures of this step key across recent runs.
                recent = db.scalars(
                    select(StepRun.status)
                    .join(WorkflowRun, StepRun.run_id == WorkflowRun.id)
                    .where(
                        WorkflowRun.workflow_id == run.workflow_id,
                        StepRun.step_key == step.step_key,
                    )
                    .order_by(StepRun.created_at.desc())
                    .limit(consecutive)
                ).all()
                if len(recent) < consecutive or any(s != "failed" for s in recent):
                    continue
            event = _fire(
                db,
                rule,
                f"Step '{step.step_key}' failed in run {run.id[:8]}",
                {"step_key": step.step_key, "error": step.error},
                workflow_id=run.workflow_id,
                run_id=run.id,
            )
            if event:
                fired.append(event)
        elif rule.condition == "run_failed":
            if run.status != "failed":
                continue
            event = _fire(
                db,
                rule,
                f"Run {run.id[:8]} of workflow failed",
                {"run_id": run.id},
                workflow_id=run.workflow_id,
                run_id=run.id,
            )
            if event:
                fired.append(event)
    return fired


def sweep_time_based_rules(db: Session) -> list[AlertEvent]:
    """Periodic sweep for no_successful_run, cost_exceeds, and budget guards."""
    fired: list[AlertEvent] = []
    now = utcnow()
    rules = db.scalars(select(AlertRule).where(AlertRule.is_active.is_(True))).all()
    for rule in rules:
        params = rule.params or {}
        if _in_cooldown(rule, now):
            continue
        if rule.condition == "no_successful_run":
            minutes = int(params.get("minutes", 60))
            since = now - timedelta(minutes=minutes)
            from app.models.workflow import Workflow

            query = (
                select(func.count())
                .select_from(WorkflowRun)
                .join(Workflow, WorkflowRun.workflow_id == Workflow.id)
                .where(
                    WorkflowRun.status == "succeeded",
                    WorkflowRun.created_at >= since,
                    WorkflowRun.is_test.is_(False),
                    Workflow.owner_id == rule.user_id,
                )
            )
            if rule.workflow_id:
                query = query.where(WorkflowRun.workflow_id == rule.workflow_id)
            count = db.scalar(query) or 0
            if count == 0:
                event = _fire(
                    db,
                    rule,
                    f"No successful run in the last {minutes} minute(s)",
                    {"minutes": minutes, "successful_runs": 0},
                    workflow_id=rule.workflow_id,
                )
                if event:
                    fired.append(event)
        elif rule.condition == "cost_exceeds":
            amount = float(params.get("amount_usd", 0))
            period = params.get("period", "day")
            since = now - timedelta(days=1 if period == "day" else 0, hours=0 if period == "day" else 1)
            if period == "run":
                # Evaluated per-run elsewhere; skip in sweep.
                continue
            if rule.workflow_id:
                total = _workflow_spend_usd(db, rule.workflow_id, since)
            else:
                total = 0.0
                for wid in db.scalars(select(WorkflowRun.workflow_id).distinct()).all():
                    total += _workflow_spend_usd(db, wid, since)
            if total >= amount:
                event = _fire(
                    db,
                    rule,
                    f"Cost ${total:.2f} exceeded ${amount:.2f} in the last {period}",
                    {"total_usd": total, "threshold_usd": amount, "period": period},
                    workflow_id=rule.workflow_id,
                )
                if event:
                    fired.append(event)
    fired.extend(check_budgets(db, now))
    return fired


def _workflow_spend_usd(db: Session, workflow_id: str, since: datetime) -> float:
    """Sum AI cost across non-test runs of a workflow since a timestamp.

    Cost is recorded per step in ``output_data.ai_usage.cost_usd``; there is
    no denormalized run-level column, so this scans step outputs. Budgets are
    checked on a sweep cadence, not per dispatch, so the scan is acceptable.
    """
    total = 0.0
    run_ids = db.scalars(
        select(WorkflowRun.id).where(
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.created_at >= since,
            WorkflowRun.is_test.is_(False),
        )
    ).all()
    for run_id in run_ids:
        outputs = db.scalars(
            select(StepRun.output_data).where(StepRun.run_id == run_id)
        ).all()
        for output in outputs:
            if isinstance(output, dict):
                usage = output.get("ai_usage")
                if isinstance(usage, dict):
                    try:
                        total += float(usage.get("cost_usd") or 0)
                    except (TypeError, ValueError):
                        pass
    return total


def check_budgets(db: Session, now: datetime | None = None) -> list[AlertEvent]:
    """Cost guard: warn at warn_at_pct, hard-stop runs at hard_stop_at_pct.

    Returns synthetic alert events (not tied to a rule) for warnings so they
    appear in /alerts. Hard-stopped workflows get their queued runs cancelled.
    """
    now = now or utcnow()
    fired: list[AlertEvent] = []
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    budgets = db.scalars(select(WorkflowBudget).where(WorkflowBudget.is_active.is_(True))).all()
    for budget in budgets:
        spent = _workflow_spend_usd(db, budget.workflow_id, month_start)
        pct = (spent / budget.monthly_budget_usd * 100) if budget.monthly_budget_usd > 0 else 0
        # Warn once per crossing.
        if pct >= budget.warn_at_pct and (
            budget.last_warned_at is None or budget.last_warned_at < month_start
        ):
            budget.last_warned_at = now
            event = AlertEvent(
                rule_id=None,
                user_id=_budget_owner(db, budget),
                workflow_id=budget.workflow_id,
                condition="budget_warning",
                message=f"Workflow spend ${spent:.2f} is {pct:.0f}% of the ${budget.monthly_budget_usd:.2f} monthly budget",
                details={"spent_usd": spent, "budget_usd": budget.monthly_budget_usd, "pct": pct},
            )
            db.add(event)
            fired.append(event)
        # Hard stop: cancel queued runs once.
        if pct >= budget.hard_stop_at_pct and budget.hard_stopped_at is None:
            budget.hard_stopped_at = now
            queued = db.scalars(
                select(WorkflowRun).where(
                    WorkflowRun.workflow_id == budget.workflow_id,
                    WorkflowRun.status.in_(("queued", "waiting_approval")),
                )
            ).all()
            for run in queued:
                run.status = "cancelled"
            event = AlertEvent(
                rule_id=None,
                user_id=_budget_owner(db, budget),
                workflow_id=budget.workflow_id,
                condition="budget_hard_stop",
                message=f"Monthly budget exhausted (${spent:.2f}); {len(queued)} queued run(s) cancelled",
                details={"spent_usd": spent, "budget_usd": budget.monthly_budget_usd, "cancelled_runs": len(queued)},
            )
            db.add(event)
            fired.append(event)
    if fired:
        db.flush()
    return fired


def _budget_owner(db: Session, budget: WorkflowBudget) -> str:
    from app.models.workflow import Workflow

    workflow = db.get(Workflow, budget.workflow_id)
    return workflow.owner_id if workflow else ""


def budget_status(db: Session, workflow_id: str, now: datetime | None = None) -> dict | None:
    """Current spend vs budget, plus a days-to-exhaustion projection."""
    now = now or utcnow()
    budget = db.scalar(select(WorkflowBudget).where(WorkflowBudget.workflow_id == workflow_id))
    if not budget or not budget.is_active:
        return None
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    spent = _workflow_spend_usd(db, workflow_id, month_start)
    days_elapsed = max(1, (now - month_start).days + 1)
    daily_rate = spent / days_elapsed
    remaining = max(0.0, budget.monthly_budget_usd - spent)
    days_left = (remaining / daily_rate) if daily_rate > 0 else None
    return {
        "monthly_budget_usd": budget.monthly_budget_usd,
        "spent_usd": round(spent, 2),
        "pct_used": round(spent / budget.monthly_budget_usd * 100, 1) if budget.monthly_budget_usd else 0,
        "warn_at_pct": budget.warn_at_pct,
        "hard_stop_at_pct": budget.hard_stop_at_pct,
        "hard_stopped": budget.hard_stopped_at is not None,
        "projected_days_to_exhaustion": round(days_left, 1) if days_left is not None else None,
    }


def dry_run_rule(db: Session, rule: AlertRule) -> dict:
    """Evaluate a rule without firing: returns whether it WOULD fire now."""
    now = utcnow()
    params = rule.params or {}
    would_fire = False
    detail: dict[str, Any] = {}
    if rule.condition == "no_successful_run":
        minutes = int(params.get("minutes", 60))
        since = now - timedelta(minutes=minutes)
        query = select(func.count()).select_from(WorkflowRun).where(
            WorkflowRun.status == "succeeded",
            WorkflowRun.created_at >= since,
            WorkflowRun.is_test.is_(False),
        )
        if rule.workflow_id:
            query = query.where(WorkflowRun.workflow_id == rule.workflow_id)
        count = db.scalar(query) or 0
        would_fire = count == 0
        detail = {"successful_runs_in_window": count, "minutes": minutes}
    elif rule.condition == "cost_exceeds":
        amount = float(params.get("amount_usd", 0))
        query = select(func.coalesce(func.sum(WorkflowRun.cost_usd), 0)).where(
            WorkflowRun.created_at >= now - timedelta(days=1),
            WorkflowRun.is_test.is_(False),
        )
        if rule.workflow_id:
            query = query.where(WorkflowRun.workflow_id == rule.workflow_id)
        total = float(db.scalar(query) or 0)
        would_fire = total >= amount
        detail = {"total_usd_24h": total, "threshold_usd": amount}
    else:
        detail = {"note": "Event-driven conditions (step_failed, run_failed) can only be evaluated on live events."}
    return {"would_fire": would_fire, "in_cooldown": _in_cooldown(rule, now), "detail": detail}
