"""Teams and membership roles for multi-tenant governance.

A team groups users and workflows. Roles are ordered by capability:

- ``viewer``: read workflows, runs, schedules and triggers.
- ``editor``: viewer + create/edit/archive workflows and schedules.
- ``operator``: editor + start/stop/retry runs, approve steps, manage workers.
- ``admin``: operator + manage team membership and settings.

Every user gets a personal team (backfilled by migration); workflows carry an
optional ``team_id`` — ``None`` means the workflow is personal to its owner.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, new_id, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.models.user import User

TEAM_ROLES = ("viewer", "editor", "operator", "admin")

# Numeric levels so "role >= required" checks stay trivial.
ROLE_LEVELS = {"viewer": 1, "editor": 2, "operator": 3, "admin": 4}


def role_at_least(role: str, required: str) -> bool:
    """True when ``role`` carries at least the capability of ``required``."""
    return ROLE_LEVELS.get(role, 0) >= ROLE_LEVELS.get(required, 99)


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    memberships: Mapped[list["TeamMembership"]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )


class TeamMembership(Base):
    __tablename__ = "team_memberships"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_membership"),)

    team: Mapped["Team"] = relationship(back_populates="memberships")
    user: Mapped["User"] = relationship(back_populates="team_memberships")
