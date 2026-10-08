"""Stage H1 test-run migration.

Revision ID: 20261010_1610_test_runs
Revises: 20261010_1600_draft_version

Adds ``is_test`` to workflow_runs. Test runs execute a draft (or a single
step) from the builder without publishing: they never fire triggers,
never send notifications, and never consume production quotas.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261010_1610_test_runs"
down_revision: Union[str, None] = "20261010_1600_draft_version"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("workflow_runs")}
    if "is_test" not in columns:
        op.add_column("workflow_runs", sa.Column("is_test", sa.Boolean(), nullable=False, server_default="0"))
    op.execute(sa.text("UPDATE workflow_runs SET is_test = FALSE WHERE is_test IS NULL"))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("workflow_runs")}
    if "is_test" in columns:
        op.drop_column("workflow_runs", "is_test")
