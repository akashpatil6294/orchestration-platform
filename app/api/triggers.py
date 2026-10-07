"""Authenticated trigger management and public signed webhook ingestion."""
from __future__ import annotations

from fastapi import APIRouter, Header, Request, Response, status

from app.api.deps import CurrentUser, DbSession
from app.core.rate_limit import trigger_limiter
from app.models.workflow import Workflow
from app.schemas.trigger import TriggerCreate, TriggerSecretRotate, TriggerUpdate, TriggerView, WebhookAccepted
from app.services import trigger_service, workflow_service

router = APIRouter(prefix="/api/v1/triggers", tags=["triggers"])
workflow_router = APIRouter(prefix="/api/v1/workflows/{workflow_id}/triggers", tags=["triggers"])
hook_router = APIRouter(prefix="/api/v1/hooks", tags=["webhooks"])


def _view(db, trigger) -> dict:
    workflow = db.get(Workflow, trigger.workflow_id)
    return trigger_service.trigger_view(
        trigger,
        workflow_name=workflow.name if workflow and workflow.owner_id == trigger.owner_id else "",
    )


@router.get("", response_model=dict)
def list_triggers(user: CurrentUser, db: DbSession) -> dict:
    items = [_view(db, trigger) for trigger in trigger_service.list_triggers(db, user.id)]
    return {"items": items, "total": len(items)}


@router.get("/{trigger_id}", response_model=TriggerView)
def get_trigger(trigger_id: str, user: CurrentUser, db: DbSession) -> dict:
    return _view(db, trigger_service.get_trigger(db, trigger_id, user.id))


@router.patch("/{trigger_id}", response_model=TriggerView)
def update_trigger(trigger_id: str, payload: TriggerUpdate, user: CurrentUser, db: DbSession) -> dict:
    trigger = trigger_service.get_trigger(db, trigger_id, user.id)
    trigger_service.update_trigger(db, trigger, payload)
    db.commit()
    db.refresh(trigger)
    return _view(db, trigger)


@router.post("/{trigger_id}/rotate-secret", response_model=TriggerView)
def rotate_secret(trigger_id: str, payload: TriggerSecretRotate, user: CurrentUser, db: DbSession) -> dict:
    trigger = trigger_service.get_trigger(db, trigger_id, user.id)
    trigger_service.rotate_trigger_secret(db, trigger, actor=user.email, secret=payload.signing_secret)
    db.commit()
    db.refresh(trigger)
    return _view(db, trigger)


@router.delete("/{trigger_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_trigger(trigger_id: str, user: CurrentUser, db: DbSession) -> Response:
    trigger = trigger_service.get_trigger(db, trigger_id, user.id)
    db.delete(trigger)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@workflow_router.post("", response_model=TriggerView, status_code=status.HTTP_201_CREATED)
def create_trigger(workflow_id: str, payload: TriggerCreate, user: CurrentUser, db: DbSession) -> dict:
    workflow = workflow_service.get_workflow(db, workflow_id, user.id)
    trigger, _secret = trigger_service.create_trigger(db, workflow, user.id, payload)
    db.commit()
    db.refresh(trigger)
    return _view(db, trigger)


def _check_trigger_rate_limit(request: Request, trigger_id: str) -> None:
    client = request.client.host if request.client else "unknown"
    allowed, retry_after = trigger_limiter().allow(f"trigger:{trigger_id}:{client}")
    if not allowed:
        from app.core.errors import RateLimited

        raise RateLimited(
            "Too many webhook deliveries, please try again later",
            code="trigger_rate_limited",
            details={"retry_after_seconds": retry_after},
        )


@hook_router.post("/{trigger_id}", response_model=WebhookAccepted, status_code=status.HTTP_202_ACCEPTED)
async def accept_webhook(
    trigger_id: str,
    request: Request,
    db: DbSession,
    timestamp: str | None = Header(default=None, alias="X-Orchestrator-Timestamp"),
    signature: str | None = Header(default=None, alias="X-Orchestrator-Signature"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    _check_trigger_rate_limit(request, trigger_id)
    body = await request.body()
    result = trigger_service.accept_webhook(
        db,
        trigger_id=trigger_id,
        body=body,
        timestamp_header=timestamp,
        signature_header=signature,
        idempotency_key=idempotency_key,
    )
    db.commit()
    return result


__all__ = ["hook_router", "router", "workflow_router"]
