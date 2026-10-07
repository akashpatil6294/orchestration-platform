"""Audit log routes: listing and CSV export (admin only)."""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse

from app.api.deps import AdminUser, CurrentUser, DbSession
from app.core.pagination import Pagination, page_of, pagination
from app.models.audit import sanitize_csv_value
from app.services import audit_service

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


def _view(event) -> dict:
    return {
        "id": event.id,
        "actor_user_id": event.actor_user_id,
        "actor_token_id": event.actor_token_id,
        "action": event.action,
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "ip_address": event.ip_address,
        "details": event.details,
        "created_at": event.created_at,
    }


@router.get("/events")
def list_audit_events(
    user: AdminUser,
    db: DbSession,
    action: str | None = Query(default=None, max_length=80),
    resource_type: str | None = Query(default=None, max_length=80),
    actor_user_id: str | None = Query(default=None, max_length=32),
    page: Pagination = Depends(pagination),
) -> dict:
    rows, total = audit_service.list_events(
        db,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        limit=page.limit,
        offset=page.offset,
    )
    return page_of([_view(row) for row in rows], total=total, page=page)


@router.get("/events.csv")
def export_audit_csv(
    user: AdminUser,
    db: DbSession,
    action: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=1000, ge=1, le=5000),
) -> PlainTextResponse:
    rows, _ = audit_service.list_events(db, action=action, limit=limit)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "created_at", "actor_user_id", "actor_token_id", "action", "resource_type", "resource_id", "ip_address"])
    for event in rows:
        writer.writerow(
            [
                sanitize_csv_value(event.id),
                sanitize_csv_value(event.created_at.isoformat() if event.created_at else ""),
                sanitize_csv_value(event.actor_user_id),
                sanitize_csv_value(event.actor_token_id),
                sanitize_csv_value(event.action),
                sanitize_csv_value(event.resource_type),
                sanitize_csv_value(event.resource_id),
                sanitize_csv_value(event.ip_address),
            ]
        )
    return PlainTextResponse(
        buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit-events.csv"},
    )


__all__ = ["router"]
