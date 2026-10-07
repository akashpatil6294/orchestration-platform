"""Stage D team connections migration.

Revision ID: 20261010_1300_connections
Revises: 20261010_1200_documents
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261010_1300_connections"
down_revision: Union[str, None] = "20261010_1200_documents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "connections" in set(sa.inspect(bind).get_table_names()):
        return
    op.create_table(
        "connections",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("team_id", sa.String(32), sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="generic"),
        sa.Column("value_ciphertext", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Index("ix_connections_owner_id", "owner_id"),
        sa.Index("ix_connections_team_id", "team_id"),
        sa.UniqueConstraint("owner_id", "name", name="uq_connection_owner_name"),
    )


def downgrade() -> None:
    if "connections" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("connections")
