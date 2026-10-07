"""Worker protocol schemas.

The worker contract is deliberately narrow: register, claim, heartbeat,
complete, fail. Task payloads include the dependency outputs a step needs, and
every task carries an idempotency key derived from the run, step and attempt so
a handler with external side effects can deduplicate an at-least-once delivery.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ConfigDict, Field

from app.schemas.base import ApiModel


class WorkerRegisterRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    name: str = Field(default="", max_length=160)
    task_types: list[str] = Field(default_factory=list, max_length=200)
    queues: list[str] = Field(default_factory=lambda: ["default"], max_length=100)
    max_concurrency: int = Field(default=1, ge=1, le=64)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerView(ApiModel):
    worker_id: str
    name: str = ""
    active: bool
    stale: bool
    task_types: list[str] = Field(default_factory=list)
    queues: list[str] = Field(default_factory=lambda: ["default"])
    queues: list[str] = Field(default_factory=lambda: ["default"])
    max_concurrency: int = 1
    last_seen_at: datetime
    created_at: datetime
    running_steps: int = 0


class WorkerRegisterResponse(ApiModel):
    worker_id: str
    active: bool
    task_types: list[str]
    queues: list[str] = Field(default_factory=lambda: ["default"])
    lease_seconds: int
    heartbeat_interval_seconds: int
    server_time: datetime


class WorkerCreateRequest(ApiModel):
    """Admin-minted worker credential. The plaintext token is returned exactly
    once in the response; only its hash is stored."""

    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    name: str = Field(default="", max_length=160)
    task_types: list[str] = Field(default_factory=list, max_length=200)
    queues: list[str] = Field(default_factory=lambda: ["default"], max_length=100)
    max_concurrency: int = Field(default=4, ge=1, le=64)


class WorkerCreateResponse(ApiModel):
    worker_id: str
    token: str = Field(description="Plaintext credential, shown once. It is not stored.")
    token_prefix: str
    active: bool
    task_types: list[str]
    queues: list[str]
    max_concurrency: int


class ClaimRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1, max_length=128)
    available_slots: int = Field(default=1, ge=1, le=64)
    task_types: list[str] | None = Field(default=None, description="Override the registered allow-list for this claim.")
    queues: list[str] | None = Field(default=None, max_length=100, description="Queue allow-list for this worker pool.")
    in_flight_task_ids: list[str] | None = Field(
        default=None,
        max_length=64,
        description="Task ids already received by this worker; unacknowledged claims can be recovered safely.",
    )
    wait_seconds: int = Field(default=0, ge=0, le=25)


class TaskAssignment(ApiModel):
    id: str
    run_id: str
    workflow_id: str
    workflow_version: int
    step_key: str
    name: str = ""
    type: str
    input: dict[str, Any] = Field(default_factory=dict)
    workflow_input: dict[str, Any] = Field(default_factory=dict)
    dependency_outputs: dict[str, Any] = Field(default_factory=dict)
    attempt: int
    retry_limit: int = 0
    timeout_seconds: int = 300
    lease_token: str
    lease_expires_at: datetime
    deadline_at: datetime
    heartbeat_interval_seconds: int
    idempotency_key: str
    priority: int = 0
    queue: str = "default"
    redacted_keys: list[str] = Field(default_factory=list)


class ClaimResponse(ApiModel):
    # ``task`` is the single-task convenience field; ``tasks`` carries every
    # assignment from a multi-slot claim. Both are always present.
    task: TaskAssignment | None = None
    tasks: list[TaskAssignment] = Field(default_factory=list)
    server_time: datetime
    retry_after_seconds: float = 1.0
    cancelled_run_ids: list[str] = Field(default_factory=list)


class LeaseRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=8, max_length=256)


class HeartbeatRequest(LeaseRequest):
    active_tasks: int = Field(default=1, ge=0, le=64)
    progress: dict[str, Any] | None = None


class HeartbeatResponse(ApiModel):
    cancel_requested: bool
    lease_expires_at: datetime
    deadline_at: datetime
    server_time: datetime
    seconds_remaining: float


class CompleteRequest(LeaseRequest):
    output: Any = None
    logs: list[dict[str, Any]] = Field(default_factory=list, max_length=500)


class FailRequest(LeaseRequest):
    error: dict[str, Any]
    retryable: bool | None = None
    logs: list[dict[str, Any]] = Field(default_factory=list, max_length=500)


class TaskResultResponse(ApiModel):
    task_id: str
    status: str
    run_status: str
    attempt: int
    retry_in_seconds: float | None = None
    message: str = ""


class WorkerShutdownResponse(ApiModel):
    worker_id: str
    active: bool
    released_tasks: int
    message: str = ""


__all__ = [
    "ClaimRequest",
    "ClaimResponse",
    "CompleteRequest",
    "FailRequest",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "LeaseRequest",
    "TaskAssignment",
    "TaskResultResponse",
    "WorkerRegisterRequest",
    "WorkerRegisterResponse",
    "WorkerShutdownResponse",
    "WorkerView",
]
