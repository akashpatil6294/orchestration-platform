"""Stage G saved run filters migration.

Revision ID: 20261010_1500_saved_filters
Revises: 20261010_1400_notification_channels
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261010_1500_saved_filters"
down_revision: Union[str, None] = "20261010_1400_notification_channels"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "saved_filters" in tables:
        return
    op.create_table(
        "saved_filters",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "saved_filters" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("saved_filters")
