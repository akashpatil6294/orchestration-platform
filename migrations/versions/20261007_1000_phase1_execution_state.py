"""persist phase-one execution policies and run controls

Revision ID: e31a7c9b4d20
Revises: b7f04c9a12de
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e31a7c9b4d20"
down_revision: Union[str, None] = "b7f04c9a12de"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    # Some installations have drifted legacy schemas recorded at the repair
    # revision. The repair migration handles their known missing policy column;
    # do not try to synthesize unrelated execution tables in that narrow case.
    if "workflow_runs" not in tables or "step_runs" not in tables or "workflows" not in tables:
        return

    run_columns = _columns("workflow_runs")
    run_additions = (
        ("definition_json", sa.Column("definition_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))),
        ("parent_run_id", sa.Column("parent_run_id", sa.String(32), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE", name="fk_workflow_runs_parent_run_id"), nullable=True)),
        ("parent_step_run_id", sa.Column("parent_step_run_id", sa.String(32), nullable=True)),
        ("nesting_depth", sa.Column("nesting_depth", sa.Integer(), nullable=False, server_default=sa.text("0"))),
        ("deadline_at", sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True)),
        ("sla_deadline_at", sa.Column("sla_deadline_at", sa.DateTime(timezone=True), nullable=True)),
        ("sla_breached_at", sa.Column("sla_breached_at", sa.DateTime(timezone=True), nullable=True)),
        ("paused_at", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True)),
    )
    missing_run_columns = [column for name, column in run_additions if name not in run_columns]
    if missing_run_columns:
        with op.batch_alter_table(
            "workflow_runs", recreate="always" if bind.dialect.name == "sqlite" else "auto"
        ) as batch_op:
            for column in missing_run_columns:
                batch_op.add_column(column)

    step_columns = _columns("step_runs")
    step_additions = (
        ("spec_json", sa.Column("spec_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))),
        ("parent_step_id", sa.Column("parent_step_id", sa.String(32), nullable=True)),
        ("foreach_index", sa.Column("foreach_index", sa.Integer(), nullable=True)),
        ("enqueued_attempt", sa.Column("enqueued_attempt", sa.Integer(), nullable=True)),
        ("child_run_id", sa.Column("child_run_id", sa.String(32), nullable=True)),
    )
    missing_step_columns = [column for name, column in step_additions if name not in step_columns]
    if missing_step_columns:
        with op.batch_alter_table(
            "step_runs", recreate="always" if bind.dialect.name == "sqlite" else "auto"
        ) as batch_op:
            for column in missing_step_columns:
                batch_op.add_column(column)
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("step_runs")}
    if "ix_step_runs_parent_step_id" not in indexes:
        op.create_index("ix_step_runs_parent_step_id", "step_runs", ["parent_step_id"])
    run_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("workflow_runs")}
    if "ix_workflow_runs_parent_run_id" not in run_indexes:
        op.create_index("ix_workflow_runs_parent_run_id", "workflow_runs", ["parent_run_id"])

    if "step_cache_entries" not in tables:
        op.create_table(
            "step_cache_entries",
            sa.Column("id", sa.String(32), primary_key=True, nullable=False),
            sa.Column("workflow_id", sa.String(32), sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False),
            sa.Column("cache_key", sa.String(64), nullable=False),
            sa.Column("output_data", sa.JSON(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("workflow_id", "cache_key", name="uq_step_cache_workflow_key"),
        )
    cache_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("step_cache_entries")}
    if "ix_step_cache_entries_workflow_id" not in cache_indexes:
        op.create_index("ix_step_cache_entries_workflow_id", "step_cache_entries", ["workflow_id"])
    if "ix_step_cache_entries_expires_at" not in cache_indexes:
        op.create_index("ix_step_cache_entries_expires_at", "step_cache_entries", ["expires_at"])

    if "run_artifacts" not in tables:
        op.create_table(
            "run_artifacts",
            sa.Column("id", sa.String(32), primary_key=True, nullable=False),
            sa.Column("run_id", sa.String(32), sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("step_run_id", sa.String(32), sa.ForeignKey("step_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("storage_backend", sa.String(16), nullable=False),
            sa.Column("storage_key", sa.String(1000), nullable=False),
            sa.Column("content_type", sa.String(120), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    artifact_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("run_artifacts")}
    if "ix_run_artifacts_run_id" not in artifact_indexes:
        op.create_index("ix_run_artifacts_run_id", "run_artifacts", ["run_id"])
    if "ix_run_artifacts_step_run_id" not in artifact_indexes:
        op.create_index("ix_run_artifacts_step_run_id", "run_artifacts", ["step_run_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "run_artifacts" in tables:
        op.drop_table("run_artifacts")
    if "step_cache_entries" in tables:
        op.drop_table("step_cache_entries")
    if "step_runs" in tables:
        indexes = {item["name"] for item in sa.inspect(bind).get_indexes("step_runs")}
        if "ix_step_runs_parent_step_id" in indexes:
            op.drop_index("ix_step_runs_parent_step_id", table_name="step_runs")
        for name in ("child_run_id", "enqueued_attempt", "foreach_index", "parent_step_id", "spec_json"):
            if name in _columns("step_runs"):
                op.drop_column("step_runs", name)
    if "workflow_runs" in tables:
        indexes = {item["name"] for item in sa.inspect(bind).get_indexes("workflow_runs")}
        if "ix_workflow_runs_parent_run_id" in indexes:
            op.drop_index("ix_workflow_runs_parent_run_id", table_name="workflow_runs")
        for name in ("paused_at", "sla_breached_at", "sla_deadline_at", "deadline_at", "nesting_depth", "parent_step_run_id", "parent_run_id", "definition_json"):
            if name in _columns("workflow_runs"):
                op.drop_column("workflow_runs", name)
