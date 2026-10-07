"""teams, membership roles and workflow team assignment

Revision ID: 20261010_0900_phase5_teams
Revises: f37bfa1c6c9f
"""

from __future__ import annotations

import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261010_0900_phase5_teams"
down_revision: Union[str, None] = "f37bfa1c6c9f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex[:32]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "teams" not in tables:
        op.create_table(
            "teams",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "team_memberships" not in tables:
        op.create_table(
            "team_memberships",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("team_id", sa.String(32), nullable=False),
            sa.Column("user_id", sa.String(32), nullable=False),
            sa.Column("role", sa.String(20), nullable=False, server_default="viewer"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["team_id"], ["teams.id"], name="fk_membership_team", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_membership_user", ondelete="CASCADE"),
            sa.UniqueConstraint("team_id", "user_id", name="uq_team_membership"),
        )
        op.create_index("ix_team_memberships_team_id", "team_memberships", ["team_id"])
        op.create_index("ix_team_memberships_user_id", "team_memberships", ["user_id"])

    # workflows.team_id (nullable; NULL = personal to the owner).
    # Guarded: synthetic/partial schemas (e.g. migration repair tests) may not
    # have the workflows table at all.
    if "workflows" in tables:
        workflow_columns = {column["name"] for column in inspector.get_columns("workflows")}
        if "team_id" not in workflow_columns:
            with op.batch_alter_table(
                "workflows",
                recreate="always" if bind.dialect.name == "sqlite" else "auto",
            ) as batch:
                batch.add_column(sa.Column("team_id", sa.String(32), nullable=True))
                batch.create_foreign_key("fk_workflows_team", "teams", ["team_id"], ["id"], ondelete="SET NULL")
            op.create_index("ix_workflows_team_id", "workflows", ["team_id"])

    # Backfill: one personal team per user (idempotent — skips users that
    # already have a personal team), then assign their workflows. Skipped when
    # the users or workflows tables are absent (partial schemas).
    if "users" not in tables or "workflows" not in tables:
        return
    users = bind.execute(sa.text("SELECT id, email, display_name FROM users")).fetchall()
    for user_id, email, display_name in users:
        label = (display_name or "").strip() or (email or "").split("@")[0] or "Personal"
        team_name = f"{label}'s team"
        existing = bind.execute(
            sa.text("SELECT id FROM teams WHERE name = :name"),
            {"name": team_name},
        ).fetchone()
        if existing:
            team_id = existing[0]
        else:
            team_id = _new_id()
            now = _utcnow()
            bind.execute(
                sa.text("INSERT INTO teams (id, name, created_at, updated_at) VALUES (:id, :name, :now, :now)"),
                {"id": team_id, "name": team_name, "now": now},
            )
        member = bind.execute(
            sa.text("SELECT id FROM team_memberships WHERE team_id = :team_id AND user_id = :user_id"),
            {"team_id": team_id, "user_id": user_id},
        ).fetchone()
        if not member:
            bind.execute(
                sa.text(
                    "INSERT INTO team_memberships (id, team_id, user_id, role, created_at) "
                    "VALUES (:id, :team_id, :user_id, 'admin', :now)"
                ),
                {"id": _new_id(), "team_id": team_id, "user_id": user_id, "now": _utcnow()},
            )
        bind.execute(
            sa.text("UPDATE workflows SET team_id = :team_id WHERE owner_id = :user_id AND team_id IS NULL"),
            {"team_id": team_id, "user_id": user_id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    workflow_columns = (
        {column["name"] for column in inspector.get_columns("workflows")} if "workflows" in tables else set()
    )
    if "team_id" in workflow_columns:
        with op.batch_alter_table(
            "workflows",
            recreate="always" if bind.dialect.name == "sqlite" else "auto",
        ) as batch:
            batch.drop_column("team_id")
    if "team_memberships" in tables:
        for index in ("ix_team_memberships_team_id", "ix_team_memberships_user_id"):
            try:
                op.drop_index(index, table_name="team_memberships")
            except Exception:
                pass
    for table in ("team_memberships", "teams"):
        if table in set(inspector.get_table_names()):
            op.drop_table(table)
