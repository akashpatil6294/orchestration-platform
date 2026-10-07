"""reconcile required policy column when the recorded schema has drifted

Revision ID: b7f04c9a12de
Revises: d8e21a6c0f34
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7f04c9a12de"
down_revision: Union[str, None] = "d8e21a6c0f34"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Ensure old StepRun rows remain required when repairing a missing column."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "step_runs" not in inspector.get_table_names():
        raise RuntimeError("Cannot reconcile step_runs.required: step_runs table is missing")

    columns = {column["name"]: column for column in inspector.get_columns("step_runs")}
    required = columns.get("required")
    if required is None:
        op.add_column(
            "step_runs",
            sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
        return

    # Repair partial/manual schema changes too. Existing NULLs get the model's
    # default policy before the not-null constraint is restored.
    needs_not_null = bool(required["nullable"])
    default = required.get("default")
    normalized_default = str(default or "").strip().lower().strip("()'\"")
    needs_true_default = normalized_default not in {"true", "1", "1::boolean"}
    if needs_not_null:
        op.execute(sa.text("UPDATE step_runs SET required = TRUE WHERE required IS NULL"))
    if needs_not_null or needs_true_default:
        with op.batch_alter_table("step_runs", schema=None) as batch_op:
            batch_op.alter_column(
                "required",
                existing_type=sa.Boolean(),
                existing_nullable=bool(required["nullable"]),
                existing_server_default=default,
                nullable=False,
                server_default=sa.true(),
            )


def downgrade() -> None:
    # The preceding revision already defines this column. This revision only
    # repairs installations whose physical schema drifted from that revision.
    pass
