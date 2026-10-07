"""Workflow definition, draft and version schemas.

These types are the contract shared by the visual editor, the API and the
worker. ``extra="forbid"`` is intentional: a typo in a step field is a bug in a
workflow definition, and silently ignoring it would hide it until runtime.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator

from app.schemas.base import ApiModel

from app.core.dag import STEP_ID_PATTERN, TASK_TYPE_PATTERN

RunStatus = Literal["queued", "running", "cancelling", "succeeded", "failed", "cancelled", "skipped", "paused"]
JoinMode = Literal["all_success", "any_success", "all_done"]
PartialFailure = Literal["fail_fast", "continue"]
OnTimeout = Literal["fail", "approve", "reject"]


class CachePolicy(ApiModel):
    model_config = ConfigDict(extra="forbid")
    ttl_seconds: int = Field(ge=1, le=30 * 24 * 3600)
    key: str = Field(default="input-hash", max_length=200)


class StepDefinition(ApiModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=STEP_ID_PATTERN)
    type: str = Field(min_length=1, max_length=200, pattern=TASK_TYPE_PATTERN)
    name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=1000)
    input: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=64)
    retries: int = Field(default=0, ge=0, le=20)
    timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    backoff_seconds: int = Field(default=2, ge=0, le=3600)
    backoff_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    retry_max_delay_seconds: int | None = Field(default=None, ge=1, le=86_400)
    retry_jitter: float | None = Field(default=None, ge=0.0, le=1.0)
    required: bool = Field(default=True)
    continue_on_error: bool = Field(default=False)
    # Editor-only hint: persisted so a graph re-opens where the user left it.
    position: dict[str, float] | None = None
    when: str | None = Field(default=None, max_length=2000)
    join: JoinMode = "all_success"
    foreach: str | None = Field(default=None, max_length=500)
    max_concurrency: int | None = Field(default=None, ge=1, le=256)
    partial_failure: PartialFailure = "fail_fast"
    cache: CachePolicy | None = None
    approvers: list[str] = Field(default_factory=list, max_length=32)
    on_timeout: OnTimeout = "fail"
    compensate: str | None = Field(default=None, max_length=128)
    priority: int = Field(default=0, ge=-1000, le=1000)
    queue: str = Field(default="default", min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")
    concurrency_key: str | None = Field(default=None, max_length=200)
    concurrency_limit: int | None = Field(default=None, ge=1, le=10000)
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=1_000_000)
    rate_limit_key: str | None = Field(default=None, max_length=200)
    if_true: str | None = Field(default=None, max_length=128)
    if_false: str | None = Field(default=None, max_length=128)

    @field_validator("depends_on")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("depends_on contains duplicates")
        return value

    @field_validator("approvers")
    @classmethod
    def _clean_approvers(cls, value: list[str]) -> list[str]:
        return [item.strip()[:160] for item in value if item.strip()]

    @field_validator("position")
    @classmethod
    def _bounded_position(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        if value is None:
            return None
        x = float(value.get("x", 0.0))
        y = float(value.get("y", 0.0))
        return {"x": max(-100_000.0, min(100_000.0, x)), "y": max(-100_000.0, min(100_000.0, y))}


class WorkflowDefinition(ApiModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    steps: list[StepDefinition] = Field(min_length=1, max_length=100)
    default_max_parallel: int = Field(default=4, ge=1, le=64)
    tags: list[str] = Field(default_factory=list, max_length=10)
    on_failure: list[StepDefinition] = Field(default_factory=list, max_length=32)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    sla_seconds: int | None = Field(default=None, ge=1, le=86_400)

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        return [tag.strip()[:40] for tag in value if tag.strip()]


class WorkflowCreate(WorkflowDefinition):
    pass


class WorkflowUpdate(WorkflowDefinition):
    pass


class WorkflowDraftPatch(ApiModel):
    """Partial update used by the editor's autosave."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    steps: list[StepDefinition] | None = None
    default_max_parallel: int | None = Field(default=None, ge=1, le=64)
    team_id: str | None = Field(default=None, max_length=32)


class ValidationIssue(ApiModel):
    code: str
    message: str
    step_id: str | None = None
    field: str | None = None


class ValidationResult(ApiModel):
    valid: bool
    errors: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class ValidateRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")
    definition: WorkflowDefinition | None = None


class PublishRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=500)


class PublishResult(ApiModel):
    workflow_id: str
    version: int
    published_at: datetime
    definition_hash: str
    summary: dict[str, Any] = Field(default_factory=dict)


class WorkflowSummary(ApiModel):
    id: str
    name: str
    description: str
    latest_version: int
    draft_version: int = 1
    step_count: int
    archived: bool
    default_max_parallel: int
    created_at: datetime
    updated_at: datetime
    run_counts: dict[str, int] = Field(default_factory=dict)
    last_run_at: datetime | None = None
    last_run_status: str | None = None
    has_draft_changes: bool = False
    schedule_count: int = 0


class WorkflowDetail(WorkflowSummary):
    draft: WorkflowDefinition
    versions: list["WorkflowVersionSummary"] = Field(default_factory=list)


class WorkflowVersionSummary(ApiModel):
    version: int
    published_at: datetime
    published_by: str | None = None
    publish_note: str = ""
    step_count: int
    definition_hash: str
    is_latest: bool = False


class WorkflowVersionDetail(WorkflowVersionSummary):
    definition: WorkflowDefinition


class SecretUpsert(ApiModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")
    value: str = Field(min_length=1, max_length=8192)


class SecretView(ApiModel):
    name: str
    updated_at: datetime
    created_at: datetime


WorkflowDetail.model_rebuild()

__all__ = [
    "CachePolicy",
    "PublishRequest",
    "PublishResult",
    "SecretUpsert",
    "SecretView",
    "StepDefinition",
    "ValidateRequest",
    "ValidationIssue",
    "ValidationResult",
    "WorkflowCreate",
    "WorkflowDefinition",
    "WorkflowDetail",
    "WorkflowDraftPatch",
    "WorkflowSummary",
    "WorkflowUpdate",
    "WorkflowVersionDetail",
    "WorkflowVersionSummary",
]
