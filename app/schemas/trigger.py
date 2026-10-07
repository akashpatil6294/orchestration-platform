"""Authenticated trigger configuration and signed webhook payloads."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from app.schemas.base import ApiModel


class TriggerCreate(ApiModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    kind: Literal["webhook", "workflow_success"] = "webhook"
    source_workflow_id: str | None = Field(default=None, min_length=1, max_length=32)
    version: int | None = Field(default=None, ge=1)
    input_mapping: dict[str, str] = Field(default_factory=dict, max_length=100)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=10000)
    signing_secret: str | None = Field(default=None, min_length=32, max_length=256)

    @model_validator(mode="after")
    def _secret_matches_kind(self):
        if not self.name.strip():
            raise ValueError("Trigger name must contain a non-space character")
        if self.kind == "webhook" and self.signing_secret is None:
            raise ValueError("Webhook triggers require a client-generated signing_secret")
        if self.kind == "workflow_success" and self.signing_secret is not None:
            raise ValueError("Workflow-success triggers do not use signing secrets")
        if self.signing_secret is not None and (self.signing_secret != self.signing_secret.strip() or len(self.signing_secret.strip()) < 32):
            raise ValueError("Signing secret must contain at least 32 non-space characters without surrounding whitespace")
        return self


class TriggerSecretRotate(ApiModel):
    model_config = ConfigDict(extra="forbid")
    signing_secret: str = Field(min_length=32, max_length=256)

    @model_validator(mode="after")
    def _valid_secret(self):
        if self.signing_secret != self.signing_secret.strip() or len(self.signing_secret.strip()) < 32:
            raise ValueError("Signing secret must contain at least 32 non-space characters without surrounding whitespace")
        return self


class TriggerUpdate(ApiModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    input_mapping: dict[str, str] | None = Field(default=None, max_length=100)
    version: int | None = Field(default=None, ge=1)
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=10000)


class TriggerView(ApiModel):
    id: str
    workflow_id: str
    workflow_name: str = ""
    kind: str
    name: str
    source_workflow_id: str | None = None
    version: int | None = None
    input_mapping: dict[str, str] = Field(default_factory=dict)
    enabled: bool
    rate_limit_per_minute: int
    endpoint: str | None = None
    created_at: datetime
    updated_at: datetime


class WebhookAccepted(ApiModel):
    trigger_id: str
    run_id: str | None = None
    status: str
    replayed: bool = False


__all__ = ["TriggerCreate", "TriggerSecretRotate", "TriggerUpdate", "TriggerView", "WebhookAccepted"]
