"""Reusable team connections: named, shareable credentials.

A connection is a named credential (Slack webhook, SQL URL, …) owned by an
account or shared with a team. Step inputs reference one via
``{"$connection": "name"}``; dispatch resolves it exactly like a workflow
secret — the plaintext never persists, is never logged, and is only visible to
a worker executing a step that references it. Unlike workflow secrets,
connections are reusable across every workflow of the owner or team.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, new_id, utcnow

CONNECTION_KINDS = ("slack_webhook", "sql_url", "generic")


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="generic")
    value_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_connection_owner_name"),)


__all__ = ["CONNECTION_KINDS", "Connection"]
