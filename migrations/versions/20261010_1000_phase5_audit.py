"""audit log table

Revision ID: 20261010_1000_phase5_audit
Revises: 20261010_0900_phase5_teams
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261010_1000_phase5_audit"
down_revision: Union[str, None] = "20261010_0900_phase5_teams"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "audit_events" in tables:
        return
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("actor_user_id", sa.String(32), nullable=True),
        sa.Column("actor_token_id", sa.String(32), nullable=True),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("resource_type", sa.String(80), nullable=True),
        sa.Column("resource_id", sa.String(64), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("details", sa.JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Index("ix_audit_events_action", "action"),
        sa.Index("ix_audit_events_actor_user_id", "actor_user_id"),
        sa.Index("ix_audit_events_created_at", "created_at"),
        sa.Index("ix_audit_actor_time", "actor_user_id", "created_at"),
        sa.Index("ix_audit_resource", "resource_type", "resource_id"),
    )


def downgrade() -> None:
    if "audit_events" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("audit_events")
