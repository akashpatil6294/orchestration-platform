"""signed triggers and interval-aware schedule backfills

Revision ID: f029ca7815d3
Revises: e31a7c9b4d20
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f029ca7815d3"
down_revision: Union[str, None] = "e31a7c9b4d20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table)
    }


def _add_columns(table: str, columns: list[sa.Column]) -> None:
    existing = _columns(table)
    missing = [
        column
        for column in columns
        if column.name not in existing
    ]

    if not missing:
        return

    with op.batch_alter_table(
        table,
        recreate="always"
        if op.get_bind().dialect.name == "sqlite"
        else "auto",
    ) as batch_op:
        for column in missing:
            batch_op.add_column(column)


def _index(
    table: str,
    name: str,
    columns: list[str],
    *,
    unique: bool = False,
) -> None:
    indexes = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes(table)
    }

    if name not in indexes:
        op.create_index(
            name,
            table,
            columns,
            unique=unique,
        )


def _widen_triggered_by() -> None:
    columns = {
        column["name"]: column
        for column in sa.inspect(op.get_bind()).get_columns(
            "workflow_runs"
        )
    }

    column = columns.get("triggered_by")

    if column is None or getattr(column["type"], "length", None) == 200:
        return

    with op.batch_alter_table(
        "workflow_runs",
        recreate="always"
        if op.get_bind().dialect.name == "sqlite"
        else "auto",
    ) as batch_op:
        batch_op.alter_column(
            "triggered_by",
            existing_type=column["type"],
            type_=sa.String(200),
            existing_nullable=True,
            nullable=True,
        )


def upgrade() -> None:
    tables = set(
        sa.inspect(op.get_bind()).get_table_names()
    )

    if not {
        "workflows",
        "workflow_runs",
        "workflow_schedules",
    }.issubset(tables):
        return

    # Widen workflow_runs.triggered_by
    _widen_triggered_by()

    # Add interval/backfill fields to workflow_runs
    _add_columns(
        "workflow_runs",
        [
            sa.Column(
                "logical_date",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "interval_start",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "interval_end",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "backfill_id",
                sa.String(32),
                nullable=True,
            ),
        ],
    )

    # workflow_runs indexes
    _index(
        "workflow_runs",
        "ix_workflow_runs_backfill_id",
        ["backfill_id"],
    )

    _index(
        "workflow_runs",
        "ix_workflow_runs_backfill_status",
        ["backfill_id", "status"],
    )

    # Add schedule interval/backfill configuration
    _add_columns(
        "workflow_schedules",
        [
            sa.Column(
                "data_interval_seconds",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "jitter_seconds",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "skip_weekends",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "skip_dates",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
            sa.Column(
                "pause_windows",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
        ],
    )

    # Create workflow_triggers
    if "workflow_triggers" not in tables:
        op.create_table(
            "workflow_triggers",
            sa.Column(
                "id",
                sa.String(32),
                primary_key=True,
                nullable=False,
            ),
            sa.Column(
                "workflow_id",
                sa.String(32),
                sa.ForeignKey(
                    "workflows.id",
                    ondelete="CASCADE",
                ),
                nullable=False,
            ),
            sa.Column(
                "owner_id",
                sa.String(32),
                sa.ForeignKey(
                    "users.id",
                    ondelete="CASCADE",
                ),
                nullable=False,
            ),
            sa.Column(
                "source_workflow_id",
                sa.String(32),
                sa.ForeignKey(
                    "workflows.id",
                    ondelete="CASCADE",
                ),
                nullable=True,
            ),
            sa.Column(
                "kind",
                sa.String(24),
                nullable=False,
            ),
            sa.Column(
                "name",
                sa.String(200),
                nullable=False,
            ),
            sa.Column(
                "secret_ciphertext",
                sa.Text(),
                nullable=True,
            ),
            sa.Column(
                "input_mapping",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
            sa.Column(
                "version",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "rate_limit_per_minute",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("60"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
        )

    # workflow_triggers indexes
    _index(
        "workflow_triggers",
        "ix_workflow_triggers_workflow_id",
        ["workflow_id"],
    )

    _index(
        "workflow_triggers",
        "ix_workflow_triggers_owner_id",
        ["owner_id"],
    )

    _index(
        "workflow_triggers",
        "ix_workflow_triggers_source_workflow_id",
        ["source_workflow_id"],
    )

    _index(
        "workflow_triggers",
        "ix_trigger_source_enabled_kind",
        ["source_workflow_id", "enabled", "kind"],
    )

    # Create trigger_deliveries
    if "trigger_deliveries" not in tables:
        op.create_table(
            "trigger_deliveries",
            sa.Column(
                "id",
                sa.String(32),
                primary_key=True,
                nullable=False,
            ),
            sa.Column(
                "trigger_id",
                sa.String(32),
                sa.ForeignKey(
                    "workflow_triggers.id",
                    ondelete="CASCADE",
                ),
                nullable=False,
            ),
            sa.Column(
                "idempotency_key",
                sa.String(200),
                nullable=False,
            ),
            sa.Column(
                "payload_hash",
                sa.String(64),
                nullable=False,
            ),
            sa.Column(
                "run_id",
                sa.String(32),
                sa.ForeignKey(
                    "workflow_runs.id",
                    ondelete="SET NULL",
                ),
                nullable=True,
            ),
            sa.Column(
                "received_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "trigger_id",
                "idempotency_key",
                name="uq_trigger_delivery_key",
            ),
        )

    # trigger_deliveries indexes
    _index(
        "trigger_deliveries",
        "ix_trigger_deliveries_trigger_id",
        ["trigger_id"],
    )

    _index(
        "trigger_deliveries",
        "ix_trigger_deliveries_received_at",
        ["received_at"],
    )

    _index(
        "trigger_deliveries",
        "ix_trigger_delivery_trigger_received",
        ["trigger_id", "received_at"],
    )

    # Create schedule_backfills
    if "schedule_backfills" not in tables:
        op.create_table(
            "schedule_backfills",
            sa.Column(
                "id",
                sa.String(32),
                primary_key=True,
                nullable=False,
            ),
            sa.Column(
                "schedule_id",
                sa.String(32),
                sa.ForeignKey(
                    "workflow_schedules.id",
                    ondelete="CASCADE",
                ),
                nullable=False,
            ),
            sa.Column(
                "owner_id",
                sa.String(32),
                sa.ForeignKey(
                    "users.id",
                    ondelete="CASCADE",
                ),
                nullable=False,
            ),
            sa.Column(
                "start_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "end_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "next_slot_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "concurrency_limit",
                sa.Integer(),
                nullable=False,
            ),
            sa.Column(
                "status",
                sa.String(20),
                nullable=False,
                server_default="running",
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
            sa.Column(
                "finished_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )

    # schedule_backfills indexes
    _index(
        "schedule_backfills",
        "ix_schedule_backfills_schedule_id",
        ["schedule_id"],
    )

    _index(
        "schedule_backfills",
        "ix_schedule_backfills_owner_id",
        ["owner_id"],
    )

    _index(
        "schedule_backfills",
        "ix_schedule_backfills_status",
        ["status"],
    )


def downgrade() -> None:
    tables = set(
        sa.inspect(op.get_bind()).get_table_names()
    )

    # Drop trigger_deliveries
    if "trigger_deliveries" in tables:
        op.drop_table("trigger_deliveries")

    # Drop workflow_triggers
    if "workflow_triggers" in tables:
        op.drop_table("workflow_triggers")

    # Drop schedule_backfills
    if "schedule_backfills" in tables:
        op.drop_table("schedule_backfills")

    # Remove workflow_runs additions
    if "workflow_runs" in tables:
        indexes = {
            item["name"]
            for item in sa.inspect(op.get_bind()).get_indexes(
                "workflow_runs"
            )
        }

        if "ix_workflow_runs_backfill_status" in indexes:
            op.drop_index(
                "ix_workflow_runs_backfill_status",
                table_name="workflow_runs",
            )

        if "ix_workflow_runs_backfill_id" in indexes:
            op.drop_index(
                "ix_workflow_runs_backfill_id",
                table_name="workflow_runs",
            )

        with op.batch_alter_table(
            "workflow_runs",
            recreate="always"
            if op.get_bind().dialect.name == "sqlite"
            else "auto",
        ) as batch_op:
            for name in (
                "backfill_id",
                "interval_end",
                "interval_start",
                "logical_date",
            ):
                if name in _columns("workflow_runs"):
                    batch_op.drop_column(name)

    # Remove workflow_schedules additions
    if "workflow_schedules" in tables:
        with op.batch_alter_table(
            "workflow_schedules",
            recreate="always"
            if op.get_bind().dialect.name == "sqlite"
            else "auto",
        ) as batch_op:
            for name in (
                "pause_windows",
                "skip_dates",
                "skip_weekends",
                "jitter_seconds",
                "data_interval_seconds",
            ):
                if name in _columns("workflow_schedules"):
                    batch_op.drop_column(name)
