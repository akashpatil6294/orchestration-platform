"""Notification channel routes: manage outbound webhook subscriptions."""
from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.models.notification import NOTIFICATION_EVENTS
from app.services import notification_service

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


class ChannelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2000)
    events: list[str] = Field(min_length=1)
    channel_type: str = Field(default="webhook", max_length=20)
    workflow_id: str | None = Field(default=None, max_length=32)


def _view(channel) -> dict:
    return {
        "id": channel.id,
        "name": channel.name,
        "channel_type": channel.channel_type or "webhook",
        "workflow_id": channel.workflow_id,
        "events": channel.events,
        "is_active": channel.is_active,
        "created_at": channel.created_at,
    }


def _delivery_view(delivery) -> dict:
    return {
        "id": delivery.id,
        "channel_id": delivery.channel_id,
        "event": delivery.event,
        "status": delivery.status,
        "status_code": delivery.status_code,
        "error": delivery.error,
        "created_at": delivery.created_at,
    }


@router.get("/events")
def list_events() -> dict:
    from app.models.notification import CHANNEL_TYPES

    return {"events": list(NOTIFICATION_EVENTS), "channel_types": list(CHANNEL_TYPES)}


@router.get("/channels")
def list_channels(user: CurrentUser, db: DbSession) -> dict:
    return {"items": [_view(c) for c in notification_service.list_channels(db, user_id=user.id)]}


@router.post("/channels", status_code=status.HTTP_201_CREATED)
def create_channel(payload: ChannelCreate, user: CurrentUser, db: DbSession) -> dict:
    try:
        channel = notification_service.create_channel(
            db,
            user_id=user.id,
            name=payload.name,
            url=payload.url,
            events=payload.events,
            channel_type=payload.channel_type,
            workflow_id=payload.workflow_id,
        )
    except ValueError as exc:
        from app.core.errors import Invalid

        raise Invalid(str(exc), code="invalid_channel")
    db.commit()
    return _view(channel)


@router.get("/deliveries")
def list_deliveries(user: CurrentUser, db: DbSession, limit: int = 50) -> dict:
    items = notification_service.list_deliveries(db, user_id=user.id, limit=limit)
    return {"items": [_delivery_view(d) for d in items]}


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_channel(channel_id: str, user: CurrentUser, db: DbSession) -> None:
    notification_service.delete_channel(db, channel_id=channel_id, user_id=user.id)
    db.commit()


__all__ = ["router"]
