"""User accounts and ownership.

Every workflow is owned by a user. Ownership is checked on every read and write
so that one account can never observe another account's workflows or runs.

Two equivalent ways to sign in exist:

* email + password, stored as a bcrypt hash in ``password_hash``;
* Google, delegated to Supabase Auth, which reports a stable subject id
  (``auth.users.id``) that is stored in ``supabase_user_id``.

``supabase_user_id`` — never the email — is the durable identity key for a
federated account, so changing a Google address does not create a second user.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_id, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.models.workflow import Workflow


class User(Base, TimestampMixin):
    __tablename__ = "users"

    # The initial schema revision created this regular index under a historical
    # "email_lower" name. Keep it represented so Alembic does not propose
    # removing an index from existing deployments during autogeneration.
    __table_args__ = (Index("ix_users_email_lower", "email"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    # NULL for accounts that only ever sign in through an external provider.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Supabase ``auth.users.id`` (a UUID). Unique so one identity maps to exactly
    # one application account; NULL for password-only accounts.
    supabase_user_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    auth_provider: Mapped[str] = mapped_column(
        String(32), default="password", server_default="password", nullable=False
    )
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    workflows: Mapped[list["Workflow"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    team_memberships: Mapped[list["TeamMembership"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash)

    @property
    def initials(self) -> str:
        source = (self.display_name or self.email).strip()
        parts = [part for part in source.replace("@", " ").replace(".", " ").split() if part]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()


__all__ = ["User"]
