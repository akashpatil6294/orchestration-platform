"""Run, step-run, attempt and event schemas.

Lease tokens are intentionally absent from every schema here: they are secrets
shared only between the API and the claiming worker.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field

from app.schemas.base import ApiModel


class StartRunRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")

    version: int | None = Field(default=None, ge=1, description="Published version to run. Defaults to the latest.")
    input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    max_parallel: int | None = Field(default=None, ge=1, le=64)
    priority: int = Field(default=0, ge=-1000, le=1000)
    queue: str = Field(default="default", min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    trigger: str = Field(default="manual", max_length=24)


class TestRunRequest(ApiModel):
    """Start a builder test run of the current draft (Stage H1)."""

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(default_factory=dict, description="Sample workflow input")
    step_id: str | None = Field(
        default=None,
        description="Run only this step, with pinned sample outputs for its dependencies",
    )
    pinned_outputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Step ID -> sample output, used as dependency outputs for a single-step test",
    )
    queue: str = Field(default="default", min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")


class RunSummary(ApiModel):
    id: str
    workflow_id: str
    parent_run_id: str | None = None
    nesting_depth: int = 0
    logical_date: datetime | None = None
    interval_start: datetime | None = None
    interval_end: datetime | None = None
    workflow_name: str = ""
    version: int
    status: str
    trigger: str
    is_test: bool = False
    priority: int = 0
    queue: str = "default"
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    step_counts: dict[str, int] = Field(default_factory=dict)
    total_steps: int = 0
    completed_steps: int = 0
    progress: float = 0.0
    cancel_requested: bool = False
    idempotent_replay: bool = False


class StepRunView(ApiModel):
    id: str
    key: str
    name: str = ""
    type: str
    status: str
    priority: int = 0
    queue: str = "default"
    depends_on: list[str] = Field(default_factory=list)
    attempts: int = 0
    retry_limit: int = 0
    required: bool = True
    timeout_seconds: int = 300
    available_at: datetime | None = None
    deadline_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    worker_id: str | None = None
    output: Any = None
    error: dict[str, Any] | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    log_lines: int = 0
    last_log_at: datetime | None = None
    downstream: list[str] = Field(default_factory=list)
    retry_at: datetime | None = None
    spec: dict[str, Any] = Field(default_factory=dict)
    parent_step_id: str | None = None
    child_run_id: str | None = None
    foreach_index: int | None = None
    wait_reason: str | None = None


class RunDetail(ApiModel):
    id: str
    workflow_id: str
    parent_run_id: str | None = None
    nesting_depth: int = 0
    logical_date: datetime | None = None
    interval_start: datetime | None = None
    interval_end: datetime | None = None
    workflow_name: str = ""
    version: int
    status: str
    trigger: str
    is_test: bool = False
    priority: int = 0
    queue: str = "default"
    input: dict[str, Any] = Field(default_factory=dict)
    output: Any = None
    error: dict[str, Any] | None = None
    cancel_requested: bool = False
    cancel_requested_at: datetime | None = None
    max_parallel: int = 4
    deadline_at: datetime | None = None
    sla_deadline_at: datetime | None = None
    sla_breached_at: datetime | None = None
    paused_at: datetime | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    steps: list[StepRunView] = Field(default_factory=list)
    step_counts: dict[str, int] = Field(default_factory=dict)
    progress: float = 0.0
    latest_event_seq: int = 0
    retryable_steps: list[str] = Field(default_factory=list)


class RunEventView(ApiModel):
    seq: int
    id: str
    type: str
    level: str = "info"
    message: str = ""
    step_run_id: str | None = None
    step_key: str | None = None
    actor: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class RunEventsResponse(ApiModel):
    items: list[RunEventView]
    latest_seq: int
    has_more: bool = False


class StepAttemptView(ApiModel):
    id: str
    attempt: int
    worker_id: str | None = None
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    output: Any = None
    error: dict[str, Any] | None = None
    log_lines: int = 0


class StepAttemptsResponse(ApiModel):
    step_run_id: str
    step_key: str
    items: list[StepAttemptView]


class CancelResponse(ApiModel):
    id: str
    status: str
    cancel_requested: bool
    message: str = ""


class RetryRunRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[str] = Field(default_factory=list, description="Step keys to retry. Empty retries every failed step.")
    reset_downstream: bool = True
    input: dict[str, Any] | None = None


class RetryRunResponse(ApiModel):
    id: str
    status: str
    retried_steps: list[str]
    reset_steps: list[str] = Field(default_factory=list)


class ReplayRunRequest(ApiModel):
    """Replay a run with edited input (Stage H3)."""

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(default_factory=dict, description="Replacement workflow input")
    version: int | None = Field(default=None, ge=1, description="Published version to replay. Defaults to the original run's version.")


class ApprovalDecisionRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = Field(default="", max_length=1000)


class ApprovalDecisionResponse(ApiModel):
    run_id: str
    step_key: str
    status: str
    actor: str
    comment: str = ""


class RunControlResponse(ApiModel):
    id: str
    status: str
    message: str = ""


class DashboardCounts(ApiModel):
    status: str
    count: int


class DashboardStats(ApiModel):
    window_hours: int
    runs_total: int
    runs_by_status: list[DashboardCounts]
    runs_started: int
    runs_finished: int
    success_rate: float
    avg_duration_seconds: float | None = None
    p95_duration_seconds: float | None = None
    step_attempts: int
    retried_steps: int
    failed_steps: int
    timed_out_steps: int
    active_workers: int
    total_workers: int
    pending_steps: int
    running_steps: int
    schedules_enabled: int
    schedules_due: int
    workflows_total: int
    latest_event_at: datetime | None = None


class WorkerLoad(ApiModel):
    worker_id: str
    name: str = ""
    active: bool
    stale: bool
    task_types: list[str] = Field(default_factory=list)
    max_concurrency: int = 1
    running_steps: int = 0
    last_seen_at: datetime
    last_seen_seconds_ago: float = 0.0


class ActivityItem(ApiModel):
    kind: str
    id: str
    run_id: str | None = None
    workflow_id: str | None = None
    workflow_name: str = ""
    status: str = ""
    label: str = ""
    detail: str = ""
    at: datetime


class AttentionItem(ApiModel):
    """One entry of the dashboard 'needs attention' queue."""

    kind: str
    id: str
    run_id: str | None = None
    workflow_id: str | None = None
    step_key: str | None = None
    severity: str = "neutral"
    label: str = ""
    detail: str = ""
    at: datetime


class TimelineBucket(ApiModel):
    bucket_start: datetime
    label: str = ""
    started: int = 0
    succeeded: int = 0
    failed: int = 0


class DurationBucket(ApiModel):
    bucket_start: datetime
    label: str = ""
    avg_duration_seconds: float | None = None


class DashboardResponse(ApiModel):
    stats: DashboardStats
    recent_runs: list[RunSummary]
    recent_activity: list[ActivityItem]
    workers: list[WorkerLoad]
    top_workflows: list[dict[str, Any]] = Field(default_factory=list)
    needs_attention: list[AttentionItem] = Field(default_factory=list)
    runs_timeline: list[TimelineBucket] = Field(default_factory=list)
    duration_trend: list[DurationBucket] = Field(default_factory=list)


__all__ = [
    "ActivityItem",
    "ApprovalDecisionRequest",
    "ApprovalDecisionResponse",
    "AttentionItem",
    "CancelResponse",
    "DashboardResponse",
    "DashboardStats",
    "DurationBucket",
    "RetryRunRequest",
    "RetryRunResponse",
    "RunControlResponse",
    "RunDetail",
    "RunEventView",
    "RunEventsResponse",
    "RunSummary",
    "StartRunRequest",
    "StepAttemptView",
    "StepAttemptsResponse",
    "StepRunView",
    "TimelineBucket",
    "WorkerLoad",
]
