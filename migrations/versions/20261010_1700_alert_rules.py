"""Alert rules engine and cost guard (Stage H, H4).

Revision ID: 20261010_1700_alert_rules
Revises: 20261010_1610_test_runs
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261010_1700_alert_rules"
down_revision = "20261010_1610_test_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("team_id", sa.String(32), sa.ForeignKey("teams.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("workflow_id", sa.String(32), sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("condition", sa.String(40), nullable=False),
        sa.Column("params", sa.JSON, nullable=False, default=dict),
        sa.Column("channel_id", sa.String(32), sa.ForeignKey("notification_channels.id", ondelete="SET NULL"), nullable=True),
        sa.Column("cooldown_seconds", sa.Integer, nullable=False, default=3600),
        sa.Column("is_active", sa.Boolean, nullable=False, default=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "alert_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("rule_id", sa.String(32), sa.ForeignKey("alert_rules.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("workflow_id", sa.String(32), sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("condition", sa.String(40), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("details", sa.JSON, nullable=False, default=dict),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(320), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "workflow_budgets",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("workflow_id", sa.String(32), sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, unique=True, index=True),
        sa.Column("monthly_budget_usd", sa.Float, nullable=False),
        sa.Column("warn_at_pct", sa.Float, nullable=False, default=80.0),
        sa.Column("hard_stop_at_pct", sa.Float, nullable=False, default=100.0),
        sa.Column("is_active", sa.Boolean, nullable=False, default=True),
        sa.Column("last_warned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hard_stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("workflow_budgets")
    op.drop_table("alert_events")
    op.drop_table("alert_rules")
