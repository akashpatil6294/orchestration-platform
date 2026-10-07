"""Owner-scoped trigger configuration and signed webhook delivery."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.core.errors import Conflict, Forbidden, Invalid, NotFound, PayloadTooLarge, RateLimited, Unauthorized
from app.core.security import decrypt_secret, encrypt_secret
from app.models.base import utcnow
from app.models.event import EventType
from app.models.run import WorkflowRun
from app.models.workflow import TriggerDelivery, Workflow, WorkflowTrigger
from app.services import event_service, run_service

INPUT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,127}$")
PATH = re.compile(r"^payload(?:\.[A-Za-z0-9_-]+)*$")


def validate_mapping(mapping: dict[str, str]) -> None:
    if len(mapping) > 100:
        raise Invalid("Input mapping may contain at most 100 fields", code="trigger_mapping_invalid")
    for key, path in mapping.items():
        if not INPUT_KEY.fullmatch(key) or not isinstance(path, str) or not PATH.fullmatch(path):
            raise Invalid(
                "Each trigger input field must map from a payload JSON path such as payload.data.id",
                code="trigger_mapping_invalid",
                details={"field": key},
            )


def map_payload(mapping: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    if not mapping:
        return payload
    result: dict[str, Any] = {}
    for field, path in mapping.items():
        node: Any = payload
        for part in path.split(".")[1:]:
            if not isinstance(node, dict) or part not in node:
                raise Invalid(f"Trigger payload is missing mapped field '{path}'", code="trigger_mapping_missing")
            node = node[part]
        result[field] = node
    return result


def get_trigger(db: Session, trigger_id: str, owner_id: str) -> WorkflowTrigger:
    trigger = db.scalar(
        select(WorkflowTrigger).where(WorkflowTrigger.id == trigger_id, WorkflowTrigger.owner_id == owner_id)
    )
    if trigger is None:
        raise NotFound("Trigger not found", code="trigger_not_found")
    return trigger


def list_triggers(db: Session, owner_id: str) -> list[WorkflowTrigger]:
    return list(
        db.scalars(
            select(WorkflowTrigger).where(WorkflowTrigger.owner_id == owner_id).order_by(WorkflowTrigger.created_at.desc())
        ).all()
    )


def create_trigger(db: Session, workflow: Workflow, owner_id: str, payload: Any) -> tuple[WorkflowTrigger, str | None]:
    mapping = dict(payload.input_mapping or {})
    validate_mapping(mapping)
    if workflow.owner_id != owner_id:
        raise NotFound("Workflow not found", code="workflow_not_found")
    if workflow.latest_version < 1:
        raise Conflict("Publish the target workflow before adding a trigger", code="no_published_version")
    if payload.version is not None:
        from app.models.workflow import WorkflowVersion

        if db.scalar(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow.id,
                WorkflowVersion.version == payload.version,
            )
        ) is None:
            raise NotFound("Published workflow version not found", code="version_not_found")

    source_id = payload.source_workflow_id
    if payload.kind == "webhook" and source_id:
        raise Invalid("Webhook triggers cannot specify a source workflow", code="trigger_source_invalid")
    if payload.kind == "workflow_success":
        if not source_id:
            raise Invalid("Workflow-success triggers require source_workflow_id", code="trigger_source_required")
        source = db.scalar(select(Workflow).where(Workflow.id == source_id, Workflow.owner_id == owner_id))
        if source is None or source.latest_version < 1:
            raise NotFound("Published source workflow not found", code="workflow_not_found")
        _ensure_no_trigger_cycle(db, source.id, workflow.id)

    secret = payload.signing_secret if payload.kind == "webhook" else None
    trigger = WorkflowTrigger(
        workflow_id=workflow.id,
        owner_id=owner_id,
        source_workflow_id=source_id,
        kind=payload.kind,
        name=payload.name.strip(),
        secret_ciphertext=encrypt_secret(secret) if secret else None,
        input_mapping=mapping,
        version=payload.version or workflow.latest_version,
        enabled=True,
        rate_limit_per_minute=payload.rate_limit_per_minute or settings.trigger_default_rate_limit_per_minute,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(trigger)
    db.flush()
    event_service.emit(
        db,
        "trigger.created",
        workflow_id=workflow.id,
        message=f"{trigger.kind.replace('_', ' ').title()} trigger created",
        payload={"trigger_id": trigger.id, "kind": trigger.kind, "name": trigger.name},
        actor=owner_id,
    )
    return trigger, secret


def _ensure_no_trigger_cycle(db: Session, source_id: str, target_id: str) -> None:
    rows = db.execute(
        select(WorkflowTrigger.source_workflow_id, WorkflowTrigger.workflow_id).where(
            WorkflowTrigger.kind == "workflow_success", WorkflowTrigger.enabled.is_(True)
        )
    ).all()
    graph: dict[str, set[str]] = {}
    for source, target in rows:
        if source:
            graph.setdefault(source, set()).add(target)
    graph.setdefault(source_id, set()).add(target_id)
    stack = [target_id]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current == source_id:
            raise Conflict("Workflow-success trigger would create a cycle", code="trigger_cycle")
        if current in seen:
            continue
        seen.add(current)
        stack.extend(graph.get(current, ()))


def update_trigger(db: Session, trigger: WorkflowTrigger, payload: Any) -> WorkflowTrigger:
    values = payload.model_dump(exclude_unset=True)
    if "input_mapping" in values and values["input_mapping"] is not None:
        validate_mapping(values["input_mapping"])
        trigger.input_mapping = values["input_mapping"]
    if "name" in values and values["name"] is not None:
        trigger.name = values["name"].strip()
    if "enabled" in values and values["enabled"] is not None:
        trigger.enabled = values["enabled"]
    if "version" in values:
        if values["version"] is None:
            raise Invalid("Trigger version must stay pinned to a published version", code="trigger_version_invalid")
        from app.models.workflow import WorkflowVersion

        if db.scalar(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == trigger.workflow_id,
                WorkflowVersion.version == values["version"],
            )
        ) is None:
            raise NotFound("Published workflow version not found", code="version_not_found")
        trigger.version = values["version"]
    if "rate_limit_per_minute" in values and values["rate_limit_per_minute"] is not None:
        trigger.rate_limit_per_minute = values["rate_limit_per_minute"]
    trigger.updated_at = utcnow()
    event_service.emit(
        db, "trigger.updated", workflow_id=trigger.workflow_id,
        message="Trigger configuration updated", payload={"trigger_id": trigger.id},
    )
    return trigger


def rotate_trigger_secret(db: Session, trigger: WorkflowTrigger, *, actor: str, secret: str) -> None:
    if trigger.kind != "webhook":
        raise Invalid("Only webhook triggers have signing secrets", code="trigger_kind_invalid")
    if not isinstance(secret, str) or len(secret) < 32 or len(secret) > 256:
        raise Invalid("Signing secret must be between 32 and 256 characters", code="trigger_secret_invalid")
    trigger.secret_ciphertext = encrypt_secret(secret)
    trigger.updated_at = utcnow()
    event_service.emit(
        db, "trigger.secret_rotated", workflow_id=trigger.workflow_id,
        message="Webhook signing secret rotated", payload={"trigger_id": trigger.id}, actor=actor,
    )


def trigger_view(trigger: WorkflowTrigger, *, workflow_name: str = "") -> dict[str, Any]:
    return {
        "id": trigger.id,
        "workflow_id": trigger.workflow_id,
        "workflow_name": workflow_name,
        "kind": trigger.kind,
        "name": trigger.name,
        "source_workflow_id": trigger.source_workflow_id,
        "version": trigger.version,
        "input_mapping": trigger.input_mapping or {},
        "enabled": trigger.enabled,
        "rate_limit_per_minute": trigger.rate_limit_per_minute,
        "endpoint": f"/api/v1/hooks/{trigger.id}" if trigger.kind == "webhook" else None,
        "created_at": trigger.created_at,
        "updated_at": trigger.updated_at,
    }


def accept_webhook(
    db: Session,
    *,
    trigger_id: str,
    body: bytes,
    timestamp_header: str | None,
    signature_header: str | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    # Serialize deliveries per trigger on PostgreSQL so a concurrent burst
    # cannot race the minute-window count beyond the configured limit.
    trigger = db.scalar(
        select(WorkflowTrigger).where(WorkflowTrigger.id == trigger_id).with_for_update()
    )
    if trigger is None or trigger.kind != "webhook" or not trigger.enabled or not trigger.secret_ciphertext:
        raise NotFound("Webhook endpoint not found", code="trigger_not_found")
    if len(body) > settings.max_request_bytes:
        raise PayloadTooLarge("Webhook body exceeds the configured request limit")
    if not idempotency_key or len(idempotency_key) > 200:
        raise Invalid("Idempotency-Key header is required (1 to 200 characters)", code="idempotency_key_required")
    try:
        timestamp = int(timestamp_header or "")
        body_text = body.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise Unauthorized("Webhook signature headers or JSON encoding are invalid", code="webhook_signature_invalid") from exc
    if abs(int(time.time()) - timestamp) > settings.webhook_max_clock_skew_seconds:
        raise Unauthorized("Webhook timestamp is outside the replay window", code="webhook_replay_window")
    try:
        secret = decrypt_secret(trigger.secret_ciphertext)
    except ValueError as exc:
        raise Unauthorized("Webhook signing key is unavailable", code="webhook_signature_invalid") from exc
    expected = hmac.new(secret.encode("utf-8"), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()
    supplied = (signature_header or "").removeprefix("sha256=")
    if not hmac.compare_digest(expected, supplied):
        raise Unauthorized("Webhook signature is invalid", code="webhook_signature_invalid")
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError as exc:
        raise Invalid("Webhook body must be valid JSON", code="webhook_json_invalid") from exc
    if not isinstance(payload, dict):
        raise Invalid("Webhook body must be a JSON object", code="webhook_json_invalid")

    payload_hash = hashlib.sha256(body).hexdigest()
    existing = db.scalar(
        select(TriggerDelivery).where(
            TriggerDelivery.trigger_id == trigger.id,
            TriggerDelivery.idempotency_key == idempotency_key,
        )
    )
    if existing:
        if existing.payload_hash != payload_hash:
            raise Conflict("This idempotency key was already used for a different webhook body", code="idempotency_conflict")
        return {"trigger_id": trigger.id, "run_id": existing.run_id, "status": "accepted", "replayed": True}

    window_start = utcnow() - timedelta(minutes=1)
    recent = int(
        db.scalar(
            select(func.count()).select_from(TriggerDelivery).where(
                TriggerDelivery.trigger_id == trigger.id,
                TriggerDelivery.received_at >= window_start,
            )
        )
        or 0
    )
    if recent >= trigger.rate_limit_per_minute:
        raise RateLimited("Webhook trigger rate limit exceeded", code="trigger_rate_limited")

    workflow = db.scalar(select(Workflow).where(Workflow.id == trigger.workflow_id, Workflow.owner_id == trigger.owner_id))
    if workflow is None or workflow.archived:
        raise NotFound("Triggered workflow not found", code="workflow_not_found")
    input_data = map_payload(trigger.input_mapping or {}, payload)
    key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
    run, _ = run_service.create_run(
        db,
        workflow,
        version=trigger.version,
        input_data=input_data,
        idempotency_key=f"hook:{trigger.id}:{key_hash}",
        trigger="webhook",
        triggered_by=f"webhook:{trigger.id}",
    )
    delivery = TriggerDelivery(
        trigger_id=trigger.id,
        idempotency_key=idempotency_key,
        payload_hash=payload_hash,
        run_id=run.id,
        received_at=utcnow(),
    )
    try:
        with db.begin_nested():
            db.add(delivery)
            db.flush()
    except IntegrityError:
        winner = db.scalar(
            select(TriggerDelivery).where(
                TriggerDelivery.trigger_id == trigger.id,
                TriggerDelivery.idempotency_key == idempotency_key,
            )
        )
        if winner is None or winner.payload_hash != payload_hash:
            raise Conflict("Webhook idempotency key conflict", code="idempotency_conflict") from None
        run = db.get(WorkflowRun, winner.run_id) if winner.run_id else run
        return {"trigger_id": trigger.id, "run_id": run.id, "status": "accepted", "replayed": True}
    event_service.emit(
        db, "webhook.accepted", run_id=run.id, workflow_id=workflow.id,
        message="Signed webhook accepted", payload={"trigger_id": trigger.id, "delivery_id": delivery.id},
    )
    return {"trigger_id": trigger.id, "run_id": run.id, "status": "accepted", "replayed": False}


def fire_workflow_success_triggers(db: Session, source_run: WorkflowRun) -> int:
    if source_run.status != "succeeded":
        return 0
    triggers = list(
        db.scalars(
            select(WorkflowTrigger)
            .join(Workflow, Workflow.id == WorkflowTrigger.workflow_id)
            .where(
                WorkflowTrigger.kind == "workflow_success",
                WorkflowTrigger.source_workflow_id == source_run.workflow_id,
                WorkflowTrigger.enabled.is_(True),
                Workflow.owner_id == WorkflowTrigger.owner_id,
                Workflow.archived.is_(False),
            )
        ).all()
    )
    started = 0
    payload = {"run_id": source_run.id, "status": source_run.status, "output": source_run.output_data or {}}
    for trigger in triggers:
        target = db.scalar(
            select(Workflow).where(
                Workflow.id == trigger.workflow_id,
                Workflow.owner_id == trigger.owner_id,
                Workflow.archived.is_(False),
            )
        )
        if target is None:
            continue
        try:
            input_data = map_payload(trigger.input_mapping or {}, payload)
        except Invalid as exc:
            event_service.emit(
                db,
                "workflow.success_trigger_skipped",
                workflow_id=target.id,
                level="warning",
                message="A workflow-success trigger mapping did not match the upstream output",
                payload={"trigger_id": trigger.id, "source_run_id": source_run.id, "code": exc.code},
            )
            continue
        run, replayed = run_service.create_run(
            db,
            target,
            version=trigger.version,
            input_data=input_data,
            idempotency_key=f"success:{trigger.id}:{source_run.id}",
            trigger="workflow_success",
            triggered_by=f"workflow:{source_run.id}",
        )
        event_service.emit(
            db,
            "workflow.success_triggered",
            run_id=run.id,
            workflow_id=target.id,
            message="A successful upstream workflow triggered this run",
            payload={"trigger_id": trigger.id, "source_run_id": source_run.id, "replayed": replayed},
        )
        started += 0 if replayed else 1
    return started


__all__ = [
    "accept_webhook",
    "create_trigger",
    "fire_workflow_success_triggers",
    "get_trigger",
    "list_triggers",
    "map_payload",
    "rotate_trigger_secret",
    "trigger_view",
    "update_trigger",
    "validate_mapping",
]
