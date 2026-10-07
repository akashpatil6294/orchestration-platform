"""drop server defaults that the models do not declare

Revision ID: f37bfa1c6c9f
Revises: 81c7e9a13b42
Create Date: 2026-10-05 13:33:10.407456+00:00

Aligns the database with the SQLAlchemy models, which declare no server
defaults for these columns. SQLite cannot ALTER COLUMN in place, so every
change runs through a batch recreate guarded on table and column existence,
keeping the migration re-runnable on both dialects.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f37bfa1c6c9f"
down_revision: Union[str, None] = "81c7e9a13b42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column, existing type, downgrade server_default)
COLUMNS: list[tuple[str, str, sa.types.TypeEngine, str]] = [
    ("schedule_backfills", "status", sa.VARCHAR(length=20), "'running'"),
    ("task_rate_buckets", "tokens", sa.DOUBLE_PRECISION(precision=53), "0"),
    ("workflow_schedules", "jitter_seconds", sa.INTEGER(), "0"),
    ("workflow_schedules", "skip_weekends", sa.BOOLEAN(), "false"),
    ("workflow_triggers", "enabled", sa.BOOLEAN(), "true"),
    ("workflow_triggers", "rate_limit_per_minute", sa.INTEGER(), "60"),
]


def _table_names() -> set[str]:
    binder = op.get_bind()
    inspector = sa.inspect(binder)
    return set(inspector.get_table_names())


def _column_names(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)}


def _alter(table: str, column: str, column_type: sa.types.TypeEngine, server_default: str | None) -> None:
    """Drop or set a server default, recreating the table when needed (SQLite)."""
    if table not in _table_names() or column not in _column_names(table):
        return
    with op.batch_alter_table(table) as batch:
        batch.alter_column(column, existing_type=column_type, server_default=server_default, existing_nullable=False)


def upgrade() -> None:
    for table, column, column_type, _ in COLUMNS:
        _alter(table, column, column_type, None)


def downgrade() -> None:
    for table, column, column_type, server_default in COLUMNS:
        _alter(table, column, column_type, sa.text(server_default))
