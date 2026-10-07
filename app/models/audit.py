"""Immutable audit log.

Every security-relevant action appends one row: who did it (user + API token
when present), what happened, which resource it touched, the client IP and a
free-form details payload. Rows are append-only — there is intentionally no
update or delete path.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, new_id, utcnow


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    actor_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    actor_token_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    resource_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_audit_actor_time", "actor_user_id", "created_at"),
        Index("ix_audit_resource", "resource_type", "resource_id"),
    )


def sanitize_csv_value(value: Any) -> str:
    """Guard against CSV formula injection: values starting with = + - @ or
    containing a line break get a leading apostrophe so spreadsheet apps treat
    them as text."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@") or "\n" in text or "\r" in text:
        return "'" + text
    return text
