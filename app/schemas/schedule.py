"""Recurring schedule schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator

from app.schemas.base import ApiModel

OverlapPolicy = Literal["skip", "allow", "cancel_previous"]


class ScheduleCreate(ApiModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=200)
    cron_expression: str = Field(min_length=1, max_length=200)
    timezone: str = Field(default="UTC", max_length=64)
    enabled: bool = True
    version: int | None = Field(default=None, ge=1)
    input: dict[str, Any] = Field(default_factory=dict)
    overlap_policy: OverlapPolicy = "skip"
    catchup: bool = False
    data_interval_seconds: int | None = Field(default=None, ge=1, le=31_536_000)
    jitter_seconds: int = Field(default=0, ge=0, le=3600)
    skip_weekends: bool = False
    skip_dates: list[str] = Field(default_factory=list, max_length=366)
    pause_windows: list[dict[str, str]] = Field(default_factory=list, max_length=32)


class ScheduleUpdate(ApiModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    cron_expression: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None
    version: int | None = Field(default=None, ge=1)
    input: dict[str, Any] | None = None
    overlap_policy: OverlapPolicy | None = None
    catchup: bool | None = None
    data_interval_seconds: int | None = Field(default=None, ge=1, le=31_536_000)
    jitter_seconds: int | None = Field(default=None, ge=0, le=3600)
    skip_weekends: bool | None = None
    skip_dates: list[str] | None = Field(default=None, max_length=366)
    pause_windows: list[dict[str, str]] | None = Field(default=None, max_length=32)


class CronPreviewRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")
    cron_expression: str = Field(min_length=1, max_length=200)
    timezone: str = Field(default="UTC", max_length=64)
    count: int = Field(default=5, ge=1, le=20)


class CronPreviewResponse(ApiModel):
    valid: bool
    error: str | None = None
    description: str = ""
    timezone: str = "UTC"
    next_runs: list[datetime] = Field(default_factory=list)


class ScheduleView(ApiModel):
    id: str
    workflow_id: str
    workflow_name: str = ""
    name: str
    cron_expression: str
    cron_description: str = ""
    timezone: str
    enabled: bool
    version: int | None = None
    effective_version: int = 0
    input: dict[str, Any] = Field(default_factory=dict)
    overlap_policy: str = "skip"
    catchup: bool = False
    data_interval_seconds: int | None = None
    jitter_seconds: int = 0
    skip_weekends: bool = False
    skip_dates: list[str] = Field(default_factory=list)
    pause_windows: list[dict[str, str]] = Field(default_factory=list)
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_run_id: str | None = None
    last_status: str | None = None
    run_count: int = 0
    last_error: str | None = None
    upcoming: list[datetime] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @field_validator("name")
    @classmethod
    def _fallback_name(cls, value: str) -> str:
        return value


class ScheduleToggleRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class ScheduleFireResponse(ApiModel):
    schedule_id: str
    run_id: str | None = None
    status: str
    message: str = ""


class ScheduleBackfillRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")
    start: datetime
    end: datetime
    concurrency_limit: int = Field(default=2, ge=1, le=64)


class ScheduleBackfillView(ApiModel):
    id: str
    schedule_id: str
    start: datetime
    end: datetime
    next_slot_at: datetime | None = None
    concurrency_limit: int
    status: str
    created_at: datetime
    finished_at: datetime | None = None


__all__ = [
    "CronPreviewRequest",
    "CronPreviewResponse",
    "ScheduleCreate",
    "ScheduleBackfillRequest",
    "ScheduleBackfillView",
    "ScheduleFireResponse",
    "ScheduleToggleRequest",
    "ScheduleUpdate",
    "ScheduleView",
]
