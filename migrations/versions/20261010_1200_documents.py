"""Stage C documents migration.

Revision ID: 20261010_1200_documents
Revises: 20261010_1100_phase6
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261010_1200_documents"
down_revision: Union[str, None] = "20261010_1100_phase6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "documents" in set(sa.inspect(bind).get_table_names()):
        return
    op.create_table(
        "documents",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("owner_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False, server_default=""),
        sa.Column("content_type", sa.String(127), nullable=False, server_default="application/pdf"),
        sa.Column("size_bytes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("storage_backend", sa.String(16), nullable=False, server_default="local"),
        sa.Column("storage_key", sa.String(512), nullable=False, server_default=""),
        sa.Column("metadata", sa.JSON, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Index("ix_documents_owner_id", "owner_id"),
    )


def downgrade() -> None:
    if "documents" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("documents")
