"""Alert rules, fired alerts, and workflow budgets (Stage H, H4)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, select

from app.api.deps import CurrentUser, DbSession
from app.core.errors import Invalid, NotFound
from app.models.alert import ALERT_CONDITIONS, AlertEvent, AlertRule, WorkflowBudget
from app.models.run import utcnow
from app.services import alert_service

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


class AlertRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    workflow_id: str | None = None
    condition: str
    params: dict[str, Any] = Field(default_factory=dict)
    channel_id: str | None = None
    cooldown_seconds: int = Field(default=3600, ge=60, le=86400)


class AlertRuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    is_active: bool | None = None
    cooldown_seconds: int | None = Field(default=None, ge=60, le=86400)
    params: dict[str, Any] | None = None
    channel_id: str | None = None


class BudgetUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    monthly_budget_usd: float = Field(gt=0)
    warn_at_pct: float = Field(default=80.0, ge=1, le=100)
    hard_stop_at_pct: float = Field(default=100.0, ge=1, le=1000)
    is_active: bool = True


def _rule_view(rule: AlertRule) -> dict:
    return {
        "id": rule.id,
        "name": rule.name,
        "workflow_id": rule.workflow_id,
        "condition": rule.condition,
        "params": rule.params,
        "channel_id": rule.channel_id,
        "cooldown_seconds": rule.cooldown_seconds,
        "is_active": rule.is_active,
        "last_fired_at": rule.last_fired_at,
        "created_at": rule.created_at,
    }


def _event_view(event: AlertEvent, rule_name: str | None) -> dict:
    return {
        "id": event.id,
        "rule_id": event.rule_id,
        "rule_name": rule_name,
        "workflow_id": event.workflow_id,
        "run_id": event.run_id,
        "condition": event.condition,
        "message": event.message,
        "details": event.details,
        "acknowledged_at": event.acknowledged_at,
        "acknowledged_by": event.acknowledged_by,
        "created_at": event.created_at,
    }


@router.get("/rules", response_model=dict)
def list_rules(user: CurrentUser, db: DbSession) -> dict:
    rules = db.scalars(
        select(AlertRule).where(AlertRule.user_id == user.id).order_by(desc(AlertRule.created_at))
    ).all()
    return {"items": [_rule_view(r) for r in rules], "conditions": list(ALERT_CONDITIONS)}


@router.post("/rules", response_model=dict, status_code=status.HTTP_201_CREATED)
def create_rule(payload: AlertRuleCreate, user: CurrentUser, db: DbSession) -> dict:
    if payload.condition not in ALERT_CONDITIONS:
        raise Invalid(f"Unknown condition '{payload.condition}'", code="unknown_condition")
    if payload.workflow_id:
        from app.services import workflow_service

        workflow_service.get_workflow(db, payload.workflow_id, user.id)
    rule = AlertRule(
        user_id=user.id,
        name=payload.name.strip(),
        workflow_id=payload.workflow_id,
        condition=payload.condition,
        params=payload.params,
        channel_id=payload.channel_id,
        cooldown_seconds=payload.cooldown_seconds,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return _rule_view(rule)


@router.patch("/rules/{rule_id}", response_model=dict)
def update_rule(rule_id: str, payload: AlertRuleUpdate, user: CurrentUser, db: DbSession) -> dict:
    rule = db.scalar(select(AlertRule).where(AlertRule.id == rule_id, AlertRule.user_id == user.id))
    if rule is None:
        raise NotFound("Alert rule not found", code="rule_not_found")
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(rule, key, value)
    db.commit()
    db.refresh(rule)
    return _rule_view(rule)


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(rule_id: str, user: CurrentUser, db: DbSession) -> None:
    rule = db.scalar(select(AlertRule).where(AlertRule.id == rule_id, AlertRule.user_id == user.id))
    if rule is None:
        raise NotFound("Alert rule not found", code="rule_not_found")
    db.delete(rule)
    db.commit()


@router.post("/rules/{rule_id}/test", response_model=dict)
def test_rule(rule_id: str, user: CurrentUser, db: DbSession) -> dict:
    """Dry-run a rule: reports whether it WOULD fire right now, without firing."""
    rule = db.scalar(select(AlertRule).where(AlertRule.id == rule_id, AlertRule.user_id == user.id))
    if rule is None:
        raise NotFound("Alert rule not found", code="rule_not_found")
    return {"rule_id": rule.id, **alert_service.dry_run_rule(db, rule)}


@router.get("", response_model=dict)
def list_alerts(
    user: CurrentUser,
    db: DbSession,
    workflow_id: str | None = Query(default=None),
    condition: str | None = Query(default=None),
    acknowledged: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    query = select(AlertEvent).where(AlertEvent.user_id == user.id)
    if workflow_id:
        query = query.where(AlertEvent.workflow_id == workflow_id)
    if condition:
        query = query.where(AlertEvent.condition == condition)
    if acknowledged is True:
        query = query.where(AlertEvent.acknowledged_at.is_not(None))
    elif acknowledged is False:
        query = query.where(AlertEvent.acknowledged_at.is_(None))
    events = db.scalars(query.order_by(desc(AlertEvent.created_at)).limit(limit)).all()
    rule_names = {
        r.id: r.name for r in db.scalars(select(AlertRule).where(AlertRule.user_id == user.id)).all()
    }
    return {
        "items": [_event_view(e, rule_names.get(e.rule_id) if e.rule_id != "__budget__" else "Cost guard") for e in events]
    }


@router.post("/{event_id}/ack", response_model=dict)
def ack_alert(event_id: str, user: CurrentUser, db: DbSession) -> dict:
    event = db.scalar(select(AlertEvent).where(AlertEvent.id == event_id, AlertEvent.user_id == user.id))
    if event is None:
        raise NotFound("Alert not found", code="alert_not_found")
    event.acknowledged_at = utcnow()
    event.acknowledged_by = user.email
    db.commit()
    db.refresh(event)
    return _event_view(event, None)


@router.put("/budgets", response_model=dict)
def upsert_budget(payload: BudgetUpsert, user: CurrentUser, db: DbSession) -> dict:
    from app.services import workflow_service

    workflow = workflow_service.get_workflow(db, payload.workflow_id, user.id)
    workflow_service.require_workflow_mutation(db, workflow, user.id)
    budget = db.scalar(select(WorkflowBudget).where(WorkflowBudget.workflow_id == payload.workflow_id))
    if budget is None:
        budget = WorkflowBudget(workflow_id=payload.workflow_id)
        db.add(budget)
    budget.monthly_budget_usd = payload.monthly_budget_usd
    budget.warn_at_pct = payload.warn_at_pct
    budget.hard_stop_at_pct = payload.hard_stop_at_pct
    budget.is_active = payload.is_active
    if payload.is_active:
        budget.hard_stopped_at = None  # re-arm
    db.commit()
    db.refresh(budget)
    return {"workflow_id": budget.workflow_id, **(alert_service.budget_status(db, budget.workflow_id) or {})}


@router.get("/budgets/{workflow_id}", response_model=dict)
def get_budget(workflow_id: str, user: CurrentUser, db: DbSession) -> dict:
    from app.services import workflow_service

    workflow_service.get_workflow(db, workflow_id, user.id)
    status_info = alert_service.budget_status(db, workflow_id)
    if status_info is None:
        raise NotFound("No budget configured for this workflow", code="budget_not_found")
    return {"workflow_id": workflow_id, **status_info}
