"""Registered workers.

Workers authenticate with a bearer token that is stored only as a salted hash.
They register the task types they can execute, claim work, heartbeat to renew
their lease, and report results. The lease token is returned to the worker only
and never appears in user-facing responses.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, new_id, utc, utcnow


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    token_prefix: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    task_types: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    queues: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["default"], nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (Index("ix_workers_active_seen", "active", "last_seen_at"),)

    @property
    def is_stale(self) -> bool:
        from app.config import settings

        last_seen_at = utc(self.last_seen_at)
        if last_seen_at is None:
            raise ValueError(f"Worker {self.id} has no last-seen timestamp")
        now = utc(utcnow())
        if now is None:
            raise RuntimeError("Could not determine the current UTC time")
        return (now - last_seen_at).total_seconds() > settings.worker_stale_seconds


class WorkerToken(Base):
    """API token for the user-facing platform (also used by the CLI)."""

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("token_hash", name="uq_api_token_hash"),)


class WorkerHeartbeat(Base):
    """Rolling heartbeat samples, used for the ops view and metrics."""

    __tablename__ = "worker_heartbeats"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    worker_id: Mapped[str] = mapped_column(ForeignKey("workers.id", ondelete="CASCADE"), index=True, nullable=False)
    active_tasks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


__all__ = ["Worker", "WorkerHeartbeat", "WorkerToken"]
