"""priority queues, portable concurrency gates and task rate buckets

Revision ID: 81c7e9a13b42
Revises: f029ca7815d3
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "81c7e9a13b42"
down_revision: Union[str, None] = "f029ca7815d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_columns(table: str, columns: list[sa.Column]) -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}
    missing = [column for column in columns if column.name not in existing]
    if not missing:
        return
    with op.batch_alter_table(
        table,
        recreate="always" if op.get_bind().dialect.name == "sqlite" else "auto",
    ) as batch:
        for column in missing:
            batch.add_column(column)


def _index(table: str, name: str, columns: list[str]) -> None:
    existing = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns)


def _drop_index(table: str, name: str) -> None:
    existing = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}
    if name in existing:
        op.drop_index(name, table_name=table)


def _drop_columns(table: str, columns: list[str]) -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}
    missing = [name for name in columns if name in existing]
    if not missing:
        return
    with op.batch_alter_table(
        table,
        recreate="always" if op.get_bind().dialect.name == "sqlite" else "auto",
    ) as batch:
        for name in missing:
            batch.drop_column(name)


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if not {"workflow_runs", "step_runs", "workers"}.issubset(tables):
        return

    _add_columns(
        "workflow_runs",
        [
            sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("queue_name", sa.String(120), nullable=False, server_default=sa.text("'default'")),
        ],
    )
    _add_columns(
        "step_runs",
        [
            sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("queue_name", sa.String(120), nullable=False, server_default=sa.text("'default'")),
            sa.Column("concurrency_key", sa.String(200), nullable=True),
            sa.Column("concurrency_limit", sa.Integer(), nullable=True),
            sa.Column("lease_expirations", sa.Integer(), nullable=False, server_default=sa.text("0")),
        ],
    )
    _add_columns(
        "workers",
        [sa.Column("queues", sa.JSON(), nullable=False, server_default=sa.text("'[\"default\"]'"))],
    )
    _index("step_runs", "ix_step_claim_priority_queue", ["status", "queue_name", "priority", "available_at"])
    _index("step_runs", "ix_step_run_queue_status", ["run_id", "queue_name", "status"])

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "concurrency_gates" not in tables:
        op.create_table(
            "concurrency_gates",
            sa.Column("key", sa.String(64), primary_key=True, nullable=False),
            sa.Column("scope", sa.String(16), nullable=False),
            sa.Column("owner_id", sa.String(32), nullable=True),
            sa.Column("queue_name", sa.String(120), nullable=True),
            sa.Column("resource_key", sa.String(200), nullable=True),
            sa.Column("active_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("capacity", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    _index("concurrency_gates", "ix_concurrency_gates_scope", ["scope", "owner_id", "queue_name"])

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "task_rate_buckets" not in tables:
        op.create_table(
            "task_rate_buckets",
            sa.Column("key", sa.String(64), primary_key=True, nullable=False),
            sa.Column("tokens", sa.Float(), nullable=False, server_default=sa.text("0")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table in ("task_rate_buckets", "concurrency_gates"):
        if table in tables:
            op.drop_table(table)

    if "step_runs" in tables:
        _drop_index("step_runs", "ix_step_claim_priority_queue")
        _drop_index("step_runs", "ix_step_run_queue_status")
        _drop_columns("step_runs", ["lease_expirations", "concurrency_limit", "concurrency_key", "queue_name", "priority"])
    if "workflow_runs" in tables:
        _drop_columns("workflow_runs", ["queue_name", "priority"])
    if "workers" in tables:
        _drop_columns("workers", ["queues"])
