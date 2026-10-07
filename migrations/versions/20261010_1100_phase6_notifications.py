"""Phase 6 notifications migration.

Revision ID: 20261010_1100_phase6_notifications
Revises: 20261010_1000_phase5_audit
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261010_1100_phase6_notifications"
down_revision: Union[str, None] = "20261010_1000_phase5_audit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "notification_channels" in set(sa.inspect(bind).get_table_names()):
        return
    op.create_table(
        "notification_channels",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("url_ciphertext", sa.Text, nullable=False),
        sa.Column("events", sa.JSON, nullable=False, server_default=sa.text("'[]'")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Index("ix_notification_channels_user_id", "user_id"),
    )


def downgrade() -> None:
    if "notification_channels" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("notification_channels")
