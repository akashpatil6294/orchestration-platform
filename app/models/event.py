"""Structured event log.

Events are the audit trail a reviewer reads when answering "what actually
happened to this run?". Payloads are JSON and always redacted before insert.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, new_id, utcnow


class RunEvent(Base):
    __tablename__ = "run_events"

    # The monotonically increasing integer primary key doubles as the event
    # sequence number, which is what run-detail polling uses to fetch deltas.
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(32), default=new_id, nullable=False, unique=True)
    # Run events carry a run_id; workflow-level events (publication, secret
    # changes) carry only a workflow_id. Keeping both in one table gives the
    # activity feed a single ordered stream to read.
    run_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True, nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    step_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    step_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    message: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    actor: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_run_events_run_seq", "run_id", "seq"),
        Index("ix_run_events_workflow_seq", "workflow_id", "seq"),
    )


# Canonical event type names. Keeping them in one place makes the API contract
# explicit and stops typos from silently splitting the event stream.
class EventType:
    WORKFLOW_CREATED = "workflow.created"
    WORKFLOW_UPDATED = "workflow.updated"
    WORKFLOW_PUBLISHED = "workflow.published"
    WORKFLOW_ARCHIVED = "workflow.archived"
    WORKFLOW_SECRET_SET = "workflow.secret_set"
    WORKFLOW_SECRET_DELETED = "workflow.secret_deleted"

    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_SUCCEEDED = "run.succeeded"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_CANCELLATION_REQUESTED = "run.cancellation_requested"
    RUN_RETRY_REQUESTED = "run.retry_requested"

    STEP_READY = "step.ready"
    STEP_CLAIMED = "step.claimed"
    STEP_HEARTBEAT = "step.heartbeat"
    STEP_SUCCEEDED = "step.succeeded"
    STEP_FAILED = "step.failed"
    STEP_RETRYING = "step.retrying"
    STEP_RETRY_SCHEDULED = "step.retry_scheduled"
    STEP_TIMEOUT = "step.timeout"
    STEP_LEASE_EXPIRED = "step.lease_expired"
    STEP_CANCELLED = "step.cancelled"
    STEP_SKIPPED = "step.skipped"

    WORKER_REGISTERED = "worker.registered"
    WORKER_SHUTDOWN = "worker.shutdown"
    WORKER_UNHEALTHY = "worker.unhealthy"

    SCHEDULE_CREATED = "schedule.created"
    SCHEDULE_UPDATED = "schedule.updated"
    SCHEDULE_DELETED = "schedule.deleted"
    SCHEDULE_TRIGGERED = "schedule.triggered"
    SCHEDULE_SKIPPED = "schedule.skipped"
    SCHEDULE_FAILED = "schedule.failed"


__all__ = ["EventType", "RunEvent"]
