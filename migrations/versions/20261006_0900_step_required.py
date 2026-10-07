"""add required policy to workflow step runs

Revision ID: d8e21a6c0f34
Revises: 7c1f9b4e2d10
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8e21a6c0f34"
down_revision: Union[str, None] = "7c1f9b4e2d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("step_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    with op.batch_alter_table("step_runs", schema=None) as batch_op:
        batch_op.drop_column("required")
