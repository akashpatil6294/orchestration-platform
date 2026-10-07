"""Workflow drafts, immutable published versions and encrypted secret values.

A workflow holds a *draft* that users edit freely. Publishing snapshots the
validated draft into an immutable ``WorkflowVersion`` row; every run pins the
exact version number it executed, so history never shifts under a user.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, new_id, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.models.run import WorkflowRun
    from app.models.schedule import WorkflowSchedule
    from app.models.user import User


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    draft: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    latest_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Optimistic concurrency for the visual builder's autosave: PATCH requires
    # If-Match: <draft_version>; every draft write bumps it by one.
    draft_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    default_max_parallel: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    owner: Mapped["User"] = relationship(back_populates="workflows")
    team: Mapped["Team | None"] = relationship()
    versions: Mapped[list["WorkflowVersion"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", order_by="WorkflowVersion.version"
    )
    runs: Mapped[list["WorkflowRun"]] = relationship(back_populates="workflow", cascade="all, delete-orphan")
    schedules: Mapped[list["WorkflowSchedule"]] = relationship(back_populates="workflow", cascade="all, delete-orphan")
    secrets: Mapped[list["WorkflowSecret"]] = relationship(back_populates="workflow", cascade="all, delete-orphan")
    triggers: Mapped[list["WorkflowTrigger"]] = relationship(
        back_populates="workflow", foreign_keys="WorkflowTrigger.workflow_id", cascade="all, delete-orphan"
    )


class WorkflowVersion(Base):
    __tablename__ = "workflow_versions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    published_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    publish_note: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    workflow: Mapped["Workflow"] = relationship(back_populates="versions")

    __table_args__ = (UniqueConstraint("workflow_id", "version", name="uq_workflow_version"),)


class WorkflowSecret(Base):
    """Encrypted values referenced from step inputs as ``{"$secret": "name"}``.

    Plaintext never leaves the process: values are encrypted with the
    ``SECRETS_ENCRYPTION_KEY`` material and redacted from logs, outputs and
    user-facing API responses.
    """

    __tablename__ = "workflow_secrets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    workflow: Mapped["Workflow"] = relationship(back_populates="secrets")

    __table_args__ = (UniqueConstraint("workflow_id", "name", name="uq_workflow_secret_name"),)


class WorkflowTrigger(Base):
    """Owner-scoped webhook or workflow-success trigger configuration."""

    __tablename__ = "workflow_triggers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True, nullable=False)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    source_workflow_id: Mapped[str | None] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(24), default="webhook", nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    secret_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_mapping: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    workflow: Mapped["Workflow"] = relationship(back_populates="triggers", foreign_keys=[workflow_id])
    source_workflow: Mapped["Workflow | None"] = relationship(foreign_keys=[source_workflow_id])
    deliveries: Mapped[list["TriggerDelivery"]] = relationship(back_populates="trigger", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_trigger_source_enabled_kind", "source_workflow_id", "enabled", "kind"),)


class TriggerDelivery(Base):
    """Dedupe ledger for accepted external webhook deliveries."""

    __tablename__ = "trigger_deliveries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    trigger_id: Mapped[str] = mapped_column(ForeignKey("workflow_triggers.id", ondelete="CASCADE"), index=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    trigger: Mapped["WorkflowTrigger"] = relationship(back_populates="deliveries")

    __table_args__ = (
        UniqueConstraint("trigger_id", "idempotency_key", name="uq_trigger_delivery_key"),
        Index("ix_trigger_delivery_trigger_received", "trigger_id", "received_at"),
    )


__all__ = ["TriggerDelivery", "Workflow", "WorkflowSecret", "WorkflowTrigger", "WorkflowVersion"]
