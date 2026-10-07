"""Stage F notification channels migration.

Revision ID: 20261010_1400_notification_channels
Revises: 20261010_1300_connections

- ``notification_channels.channel_type``: webhook (default), slack, email.
- ``notification_deliveries``: append-only log of every dispatch attempt.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261010_1400_notification_channels"
down_revision: Union[str, None] = "20261010_1300_connections"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "notification_channels" not in tables or "users" not in tables or "workflows" not in tables:
        # Partial-schema repair path (see tests/test_migrations.py): the
        # referenced tables do not exist here, nothing to alter.
        return
    op.add_column(
        "notification_channels",
        sa.Column("channel_type", sa.String(20), nullable=False, server_default="webhook"),
    )
    with op.batch_alter_table("notification_channels", recreate="always") as batch:
        batch.add_column(sa.Column("workflow_id", sa.String(32), nullable=True))
        batch.create_foreign_key(
            "fk_notification_channels_workflow", "workflows", ["workflow_id"], ["id"], ondelete="CASCADE"
        )
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("channel_id", sa.String(32), sa.ForeignKey("notification_channels.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "notification_deliveries" in tables:
        op.drop_table("notification_deliveries")
    if "notification_channels" not in tables:
        return
    with op.batch_alter_table("notification_channels", recreate="always") as batch:
        batch.drop_constraint("fk_notification_channels_workflow", type_="foreignkey")
        batch.drop_column("workflow_id")
    op.drop_column("notification_channels", "channel_type")
