"""Declarative base and shared column helpers for all models."""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Naive UTC timestamp.

    Timestamps are stored as timezone-naive UTC values so that SQLite and
    PostgreSQL behave identically. API responses re-attach ``timezone.utc``.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return uuid.uuid4().hex


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


class Base(DeclarativeBase):
    """Shared declarative base."""


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


def utc(value: datetime | None) -> datetime | None:
    """Attach UTC tzinfo to a stored naive timestamp for serialization."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = ["Base", "TimestampMixin", "new_id", "new_token", "utc", "utcnow"]
