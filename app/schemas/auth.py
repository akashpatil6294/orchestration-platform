"""Authentication schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import ConfigDict, EmailStr, Field, field_validator

from app.schemas.base import ApiModel

MIN_PASSWORD_LENGTH = 10


class RegisterRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)
    display_name: str = Field(default="", max_length=120)

    @field_validator("password")
    @classmethod
    def _strength(cls, value: str) -> str:
        if value.lower() in {"password12", "password123", "letmein123", "changeme123"}:
            raise ValueError("Choose a less predictable password")
        if not any(char.isalpha() for char in value) or not any(char.isdigit() for char in value):
            raise ValueError("Password must contain at least one letter and one number")
        return value


class LoginRequest(ApiModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class UserView(ApiModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    display_name: str
    is_admin: bool
    created_at: datetime
    initials: str = "?"
    # Display-only profile fields. Never used for authorisation.
    avatar_url: str | None = None
    auth_provider: str = "password"
    has_password: bool = True


class TokenResponse(ApiModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserView


class ApiTokenCreate(ApiModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=list, max_length=10)


class ApiTokenView(ApiModel):
    id: str
    name: str
    token_prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiTokenCreated(ApiTokenView):
    """Returned exactly once, immediately after creation."""

    token: str


class WorkerTokenCreated(ApiModel):
    worker_id: str
    token: str
    token_prefix: str
    task_types: list[str]
    created_at: datetime


class SessionInfo(ApiModel):
    user: UserView
    permissions: list[str] = Field(default_factory=list)
    environment: str = "development"
    features: dict[str, Any] = Field(default_factory=dict)
    # How this request authenticated: "google" for a Supabase session token,
    # "password" for an application session token.
    sign_in_method: str = "password"


__all__ = [
    "ApiTokenCreate",
    "ApiTokenCreated",
    "ApiTokenView",
    "LoginRequest",
    "RegisterRequest",
    "SessionInfo",
    "TokenResponse",
    "UserView",
    "WorkerTokenCreated",
]
