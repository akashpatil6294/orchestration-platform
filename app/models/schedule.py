"""Recurring schedules.

Schedules are timezone-aware. ``next_run_at`` is computed in the schedule's own
timezone and stored as naive UTC. Each fire is recorded as a unique
``last_fired_slot`` so a scheduler restart, or two scheduler replicas racing,
cannot enqueue the same occurrence twice.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, new_id, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.models.workflow import Workflow


class WorkflowSchedule(Base):
    __tablename__ = "workflow_schedules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True, nullable=False)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    cron_expression: Mapped[str] = mapped_column(String(200), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int | None] = mapped_column(nullable=True)
    input_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    overlap_policy: Mapped[str] = mapped_column(String(24), default="skip", nullable=False)
    catchup: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    data_interval_seconds: Mapped[int | None] = mapped_column(nullable=True)
    jitter_seconds: Mapped[int] = mapped_column(default=0, nullable=False)
    skip_weekends: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    skip_dates: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    pause_windows: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fired_slot: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    run_count: Mapped[int] = mapped_column(default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    workflow: Mapped["Workflow"] = relationship(back_populates="schedules")

    __table_args__ = (
        UniqueConstraint("workflow_id", "cron_expression", "timezone", name="uq_schedule_identity"),
        Index("ix_schedule_due", "enabled", "next_run_at"),
    )


class ScheduleBackfill(Base):
    """Durable bounded-concurrency replay job for a logical date range."""

    __tablename__ = "schedule_backfills"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    schedule_id: Mapped[str] = mapped_column(ForeignKey("workflow_schedules.id", ondelete="CASCADE"), index=True, nullable=False)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_slot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    concurrency_limit: Mapped[int] = mapped_column(default=2, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="running", index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["ScheduleBackfill", "WorkflowSchedule"]
