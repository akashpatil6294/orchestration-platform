"""Audit log service: append-only event recording and listing."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent


def record(
    db: Session,
    *,
    action: str,
    actor_user_id: str | None = None,
    actor_token_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    ip_address: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one audit event. There is no update or delete path by design."""
    event = AuditEvent(
        actor_user_id=actor_user_id,
        actor_token_id=actor_token_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        ip_address=ip_address,
        details=details or {},
    )
    db.add(event)
    db.flush()
    return event


def list_events(
    db: Session,
    *,
    actor_user_id: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AuditEvent], int]:
    query = select(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
    count_query = select(func.count()).select_from(AuditEvent)
    if actor_user_id:
        query = query.where(AuditEvent.actor_user_id == actor_user_id)
        count_query = count_query.where(AuditEvent.actor_user_id == actor_user_id)
    if action:
        query = query.where(AuditEvent.action == action)
        count_query = count_query.where(AuditEvent.action == action)
    if resource_type:
        query = query.where(AuditEvent.resource_type == resource_type)
        count_query = count_query.where(AuditEvent.resource_type == resource_type)
    total = db.scalar(count_query) or 0
    rows = list(db.scalars(query.limit(limit).offset(offset)).all())
    return rows, total


__all__ = ["list_events", "record"]
