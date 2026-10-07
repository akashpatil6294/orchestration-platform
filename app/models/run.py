"""Runs, step runs and per-attempt history.

Execution is at-least-once: a claim carries an expiring lease, and an expired
lease is retried according to the step's retry policy. Every claim produces a
``StepAttempt`` row so a reviewer can see exactly how many times a step ran and
what each attempt produced.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, new_id, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.models.workflow import Workflow


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True, nullable=False)
    parent_run_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE", name="fk_workflow_runs_parent_run_id"), nullable=True, index=True)
    parent_step_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    nesting_depth: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    logical_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    interval_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    interval_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    backfill_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True, nullable=False)
    trigger: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)
    # Test runs execute a draft from the builder: no triggers, no
    # notifications, no production quota consumption (Stage H1).
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    input_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    output_data: Mapped[Any] = mapped_column(JSON, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_parallel: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    queue_name: Mapped[str] = mapped_column(String(120), default="default", server_default=text("'default'"), nullable=False)
    schedule_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    triggered_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    definition_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default=text("'{}'"), nullable=False)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sla_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sla_breached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workflow: Mapped["Workflow"] = relationship(back_populates="runs")
    steps: Mapped[list["StepRun"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="StepRun.step_key"
    )

    __table_args__ = (
        Index("ix_run_workflow_idempotency", "workflow_id", "idempotency_key", unique=True),
        Index("ix_run_status_created", "status", "created_at"),
        Index("ix_workflow_runs_backfill_status", "backfill_id", "status"),
    )


class StepRun(Base):
    __tablename__ = "step_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True, nullable=False)
    step_key: Mapped[str] = mapped_column(String(128), nullable=False)
    task_type: Mapped[str] = mapped_column(String(200), index=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    queue_name: Mapped[str] = mapped_column(String(120), default="default", server_default=text("'default'"), nullable=False)
    concurrency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    concurrency_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lease_expirations: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    input_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    depends_on: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # Immutable execution policy snapshot. Input values are intentionally kept
    # in input_data so secrets can be scrubbed independently after execution.
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default=text("'{}'"), nullable=False)
    parent_step_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    child_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    foreach_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_limit: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    backoff_seconds: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    backoff_multiplier: Mapped[float] = mapped_column(default=2.0, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enqueued_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    lease_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    output_data: Mapped[Any] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    logs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    run: Mapped["WorkflowRun"] = relationship(back_populates="steps")
    attempts_history: Mapped[list["StepAttempt"]] = relationship(
        back_populates="step", cascade="all, delete-orphan", order_by="StepAttempt.attempt_no"
    )

    __table_args__ = (
        UniqueConstraint("run_id", "step_key", name="uq_step_run_key"),
        Index("ix_step_claimable", "status", "available_at"),
        Index("ix_step_claim_priority_queue", "status", "queue_name", "priority", "available_at"),
        Index("ix_step_run_queue_status", "run_id", "queue_name", "status"),
    )


class StepAttempt(Base):
    __tablename__ = "step_attempts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    step_run_id: Mapped[str] = mapped_column(ForeignKey("step_runs.id", ondelete="CASCADE"), index=True, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="running", nullable=False)
    output_data: Mapped[Any] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    logs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    step: Mapped["StepRun"] = relationship(back_populates="attempts_history")

    __table_args__ = (UniqueConstraint("step_run_id", "attempt_no", name="uq_step_attempt_no"),)


class OutboxMessage(Base):
    """Transactional outbox for worker dispatch.

    A row is written in the *same* transaction as the state change that makes a
    step claimable. The relay publishes unpublished rows to Redis and marks them
    published, so a database commit and a queue message can never drift apart.
    Without ``REDIS_URL`` the table is simply a durable dispatch journal that
    workers poll past.
    """

    __tablename__ = "outbox_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    topic: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    __table_args__ = (Index("ix_outbox_pending", "published_at", "created_at"),)


class StepCacheEntry(Base):
    """Successful, bounded-lifetime results for opt-in step memoization."""

    __tablename__ = "step_cache_entries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True, nullable=False)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    output_data: Mapped[Any] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("workflow_id", "cache_key", name="uq_step_cache_workflow_key"),)


class ConcurrencyGate(Base):
    """Transactional capacity counter for global, queue, owner and resource limits."""

    __tablename__ = "concurrency_gates"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    queue_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    resource_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    active_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (Index("ix_concurrency_gates_scope", "scope", "owner_id", "queue_name"),)


class TaskRateBucket(Base):
    """Owner-scoped token bucket for task-type or connector dispatch limits."""

    __tablename__ = "task_rate_buckets"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    tokens: Mapped[float] = mapped_column(default=0.0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RunArtifact(Base):
    """Large JSON outputs held outside the relational database."""

    __tablename__ = "run_artifacts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True, nullable=False)
    step_run_id: Mapped[str] = mapped_column(ForeignKey("step_runs.id", ondelete="CASCADE"), index=True, nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), default="application/json", nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


__all__ = ["OutboxMessage", "RunArtifact", "StepAttempt", "StepCacheEntry", "StepRun", "WorkflowRun"]
