"""Stage H1 draft concurrency migration.

Revision ID: 20261010_1600_draft_version
Revises: 20261010_1500_saved_filters

Adds a ``draft_version`` counter to workflows. PATCH /api/v1/workflows/{id}
requires ``If-Match: <draft_version>``; a mismatch returns 409 with the
current draft and version so the builder can show a conflict dialog instead
of silently overwriting another editor's changes.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261010_1600_draft_version"
down_revision: Union[str, None] = "20261010_1500_saved_filters"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("workflows")}
    if "draft_version" not in columns:
        op.add_column("workflows", sa.Column("draft_version", sa.Integer(), nullable=False, server_default="1"))
    # Backfill: existing rows get version 1 via the server default; make it
    # explicit for databases that need it.
    op.execute("UPDATE workflows SET draft_version = 1 WHERE draft_version IS NULL")


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("workflows")}
    if "draft_version" in columns:
        op.drop_column("workflows", "draft_version")
