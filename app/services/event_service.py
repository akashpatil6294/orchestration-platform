"""Structured events.

Two scopes share one ordered table:

* **Run events** (``run_id`` set) describe run and step lifecycle — what a
  reviewer reads to answer "what happened to this run?".
* **Workflow events** (``workflow_id`` set, ``run_id`` null) describe
  publication and configuration changes, which feed the activity stream.

Every payload is redacted before it is written, so tokens and secret values can
never be recovered from the event log.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import redact
from app.models.event import EventType, RunEvent
from app.models.run import StepRun

MAX_MESSAGE_LENGTH = 500


def emit(
    db: Session,
    event_type: str,
    *,
    run_id: str | None = None,
    workflow_id: str | None = None,
    message: str = "",
    payload: dict[str, Any] | None = None,
    step: StepRun | None = None,
    step_run_id: str | None = None,
    step_key: str | None = None,
    level: str = "info",
    actor: str | None = None,
) -> RunEvent:
    """Append an event. The caller owns the transaction; events commit with it."""
    event = RunEvent(
        run_id=run_id,
        workflow_id=workflow_id,
        event_type=event_type,
        level=level,
        message=message[:MAX_MESSAGE_LENGTH],
        payload=redact(payload or {}),
        step_run_id=step.id if step is not None else step_run_id,
        step_key=step.step_key if step is not None else step_key,
        actor=actor,
    )
    db.add(event)
    return event


def emit_many(db: Session, run_id: str, events: Iterable[tuple[str, dict[str, Any]]]) -> None:
    for event_type, payload in events:
        emit(db, event_type, run_id=run_id, payload=payload)


def latest_seq(db: Session, run_id: str) -> int:
    return int(db.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)) or 0)


def list_events(
    db: Session,
    run_id: str,
    *,
    after_seq: int = 0,
    limit: int = 200,
    level: str | None = None,
    step_key: str | None = None,
) -> list[RunEvent]:
    query = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
    if level:
        query = query.where(RunEvent.level == level)
    if step_key:
        query = query.where(RunEvent.step_key == step_key)
    return list(db.scalars(query.order_by(RunEvent.seq).limit(limit)).all())


def list_workflow_events(db: Session, workflow_id: str, *, limit: int = 50) -> list[RunEvent]:
    return list(
        db.scalars(
            select(RunEvent)
            .where(RunEvent.workflow_id == workflow_id)
            .order_by(RunEvent.seq.desc())
            .limit(limit)
        ).all()
    )


def list_recent(db: Session, *, since: datetime | None = None, limit: int = 20) -> list[RunEvent]:
    query = select(RunEvent)
    if since is not None:
        query = query.where(RunEvent.created_at >= since)
    return list(db.scalars(query.order_by(RunEvent.seq.desc()).limit(limit)).all())


def count_events(db: Session, run_id: str) -> int:
    return int(db.scalar(select(func.count()).select_from(RunEvent).where(RunEvent.run_id == run_id)) or 0)


def count_since(db: Session, event_types: Iterable[str], *, hours: int = 24) -> int:
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    return int(
        db.scalar(
            select(func.count())
            .select_from(RunEvent)
            .where(RunEvent.event_type.in_(list(event_types)), RunEvent.created_at >= since)
        )
        or 0
    )


__all__ = [
    "EventType",
    "count_events",
    "count_since",
    "emit",
    "emit_many",
    "latest_seq",
    "list_events",
    "list_recent",
    "list_workflow_events",
]
