"""Outbound notification channels.

Users register webhook URLs subscribed to run lifecycle events. When a run
reaches a terminal state or needs approval, the platform POSTs a signed JSON
payload to each subscribed channel. URLs are stored encrypted.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, new_id, utcnow

NOTIFICATION_EVENTS = (
    "run.started",
    "run.succeeded",
    "run.failed",
    "run.cancelled",
    "run.waiting_approval",
)


class NotificationChannel(Base):
    __tablename__ = "notification_channels"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(20), nullable=False, default="webhook")
    workflow_id: Mapped[str | None] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), nullable=True)
    url_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    events: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)




CHANNEL_TYPES = ("webhook", "slack", "email")


class NotificationDelivery(Base):
    """Append-only log of notification dispatch attempts."""

    __tablename__ = "notification_deliveries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    channel_id: Mapped[str] = mapped_column(ForeignKey("notification_channels.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # delivered | failed
    status_code: Mapped[int | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


__all__ = ["CHANNEL_TYPES", "NOTIFICATION_EVENTS", "NotificationChannel", "NotificationDelivery"]
