"""Outbound webhook notifications for run lifecycle events."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.core.security import decrypt_secret, encrypt_secret
from app.models.notification import NOTIFICATION_EVENTS, NotificationChannel

logger = logging.getLogger(__name__)


def create_channel(
    db: Session,
    *,
    user_id: str,
    name: str,
    url: str,
    events: list[str],
    channel_type: str = "webhook",
    workflow_id: str | None = None,
) -> NotificationChannel:
    from app.models.notification import CHANNEL_TYPES

    invalid = [e for e in events if e not in NOTIFICATION_EVENTS]
    if invalid:
        raise ValueError(f"Unknown notification events: {invalid}")
    if channel_type not in CHANNEL_TYPES:
        raise ValueError(f"Unknown channel type '{channel_type}'")
    if channel_type == "email":
        if "@" not in url or "\n" in url or "\r" in url:
            raise ValueError("Email channel target must be a recipient address")
    elif channel_type == "slack":
        if not url.startswith("https://hooks.slack.com/"):
            raise ValueError("Slack channel URL must be a hooks.slack.com webhook URL")
    elif not url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        raise ValueError("Webhook URL must use HTTPS (or localhost for development)")
    if workflow_id is not None:
        from app.models.workflow import Workflow

        workflow = db.get(Workflow, workflow_id)
        if workflow is None or workflow.owner_id != user_id:
            raise ValueError("Workflow not found")
    channel = NotificationChannel(
        user_id=user_id,
        name=name,
        channel_type=channel_type,
        workflow_id=workflow_id,
        url_ciphertext=encrypt_secret(url),
        events=events,
    )
    db.add(channel)
    db.flush()
    return channel


def list_channels(db: Session, *, user_id: str) -> list[NotificationChannel]:
    return list(
        db.scalars(
            select(NotificationChannel)
            .where(NotificationChannel.user_id == user_id)
            .order_by(NotificationChannel.created_at.desc())
        ).all()
    )


def delete_channel(db: Session, *, channel_id: str, user_id: str) -> None:
    channel = db.get(NotificationChannel, channel_id)
    if channel is None or channel.user_id != user_id:
        raise NotFound("Notification channel not found", code="channel_not_found")
    db.delete(channel)
    db.flush()


def _sign(payload: bytes, channel_id: str) -> str:
    # HMAC with the channel id as key material; receivers verify via the
    # X-Orchestrator-Signature header.
    return hmac.new(channel_id.encode(), payload, hashlib.sha256).hexdigest()


def _record_delivery(
    db: Session,
    *,
    channel: NotificationChannel,
    event: str,
    status: str,
    status_code: int | None = None,
    error: str | None = None,
) -> None:
    from app.models.base import new_id, utcnow
    from app.models.notification import NotificationDelivery

    db.add(
        NotificationDelivery(
            id=new_id(),
            channel_id=channel.id,
            user_id=channel.user_id,
            event=event,
            status=status,
            status_code=status_code,
            error=(error or "")[:500] or None,
            created_at=utcnow(),
        )
    )


def _deliver_webhook(channel: NotificationChannel, url: str, event: str, payload: dict[str, Any], timeout_seconds: float) -> tuple[bool, int | None, str | None]:
    from app.worker.connectors import _bracket_safe_no_proxy

    with _bracket_safe_no_proxy():
        return _deliver_webhook_inner(channel, url, event, payload, timeout_seconds)


def _deliver_webhook_inner(channel: NotificationChannel, url: str, event: str, payload: dict[str, Any], timeout_seconds: float) -> tuple[bool, int | None, str | None]:
    body = json.dumps(
        {
            "event": event,
            "channel_id": channel.id,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
    ).encode()
    try:
        response = httpx.post(
            url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Orchestrator-Signature": _sign(body, channel.id),
                "X-Orchestrator-Event": event,
            },
            timeout=timeout_seconds,
        )
        if 200 <= response.status_code < 300:
            return True, response.status_code, None
        return False, response.status_code, f"HTTP {response.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, None, f"{type(exc).__name__}: {exc}"


def _deliver_slack(channel: NotificationChannel, url: str, event: str, payload: dict[str, Any], timeout_seconds: float) -> tuple[bool, int | None, str | None]:
    from app.worker.connectors import _bracket_safe_no_proxy

    with _bracket_safe_no_proxy():
        return _deliver_slack_inner(url, event, payload, timeout_seconds)


def _deliver_slack_inner(url: str, event: str, payload: dict[str, Any], timeout_seconds: float) -> tuple[bool, int | None, str | None]:
    text = f"*{event}* — {payload.get('workflow_name', 'workflow')} run {payload.get('run_id', '')} {payload.get('status', '')}".strip()
    try:
        response = httpx.post(url, json={"text": text}, timeout=timeout_seconds)
        if 200 <= response.status_code < 300:
            return True, response.status_code, None
        return False, response.status_code, f"HTTP {response.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, None, f"{type(exc).__name__}: {exc}"


def _deliver_email(channel: NotificationChannel, address: str, event: str, payload: dict[str, Any], timeout_seconds: float) -> tuple[bool, int | None, str | None]:
    from email.message import EmailMessage

    from app.config import settings

    if not settings.smtp_host:
        return False, None, "SMTP_HOST is not configured"
    message = EmailMessage()
    message["From"] = settings.smtp_from or f"orchestrator@{settings.smtp_host}"
    message["To"] = address
    message["Subject"] = f"[{event}] {payload.get('workflow_name', 'workflow')} run {payload.get('status', '')}".strip()
    message.set_content(json.dumps({"event": event, **payload}, indent=2, default=str))
    try:
        import smtplib

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port or 25, timeout=timeout_seconds) as smtp:
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password or "")
            smtp.send_message(message)
        return True, None, None
    except Exception as exc:  # noqa: BLE001
        return False, None, f"{type(exc).__name__}: {exc}"


def dispatch_event(
    db: Session,
    *,
    event: str,
    owner_id: str,
    payload: dict[str, Any],
    timeout_seconds: float = 10.0,
    workflow_id: str | None = None,
) -> int:
    """Deliver the event to every active channel subscribed to it.

    Returns the number of successful deliveries. Failures are logged and
    recorded in ``notification_deliveries``, never raised."""
    if event not in NOTIFICATION_EVENTS:
        return 0
    channels = db.scalars(
        select(NotificationChannel).where(
            NotificationChannel.user_id == owner_id,
            NotificationChannel.is_active.is_(True),
        )
    ).all()
    delivered = 0
    for channel in channels:
        if event not in (channel.events or []):
            continue
        if channel.workflow_id is not None and channel.workflow_id != workflow_id:
            continue
        try:
            target = decrypt_secret(channel.url_ciphertext)
        except ValueError:
            logger.warning("Skipping channel %s: target failed to decrypt", channel.id)
            _record_delivery(db, channel=channel, event=event, status="failed", error="decryption failed")
            continue
        channel_type = channel.channel_type or "webhook"
        if channel_type == "slack":
            ok, status_code, error = _deliver_slack(channel, target, event, payload, timeout_seconds)
        elif channel_type == "email":
            ok, status_code, error = _deliver_email(channel, target, event, payload, timeout_seconds)
        else:
            ok, status_code, error = _deliver_webhook(channel, target, event, payload, timeout_seconds)
        from app.core.metrics import counter as _counter

        labels = {"channel_type": channel_type, "event": event, "status": "delivered" if ok else "failed"}
        _counter("orchestrator_notification_deliveries_total", labels)
        if ok:
            delivered += 1
            _record_delivery(db, channel=channel, event=event, status="delivered", status_code=status_code)
        else:
            logger.warning("Notification to %s failed: %s", channel.id, error)
            _record_delivery(db, channel=channel, event=event, status="failed", status_code=status_code, error=error)
    db.flush()
    return delivered


def list_deliveries(db: Session, *, user_id: str, limit: int = 50) -> list:
    from app.models.notification import NotificationDelivery

    return list(
        db.scalars(
            select(NotificationDelivery)
            .where(NotificationDelivery.user_id == user_id)
            .order_by(NotificationDelivery.created_at.desc())
            .limit(min(max(limit, 1), 200))
        ).all()
    )


__all__ = [
    "NOTIFICATION_EVENTS",
    "create_channel",
    "delete_channel",
    "dispatch_event",
    "list_channels",
    "list_deliveries",
]
