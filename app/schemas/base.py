"""Shared schema base.

Every API response serialises timestamps with an explicit UTC offset. Timestamps
are stored as timezone-naive UTC (so SQLite and PostgreSQL behave identically),
and this base re-attaches ``Z`` on the way out so a client can never mistake a
naive value for local time.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer


class ApiModel(BaseModel):
    """Base for every request/response schema."""

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("*", when_used="json")
    def _serialize_utc(self, value: Any, _info: Any) -> Any:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if isinstance(value, date):
            return value.isoformat()
        return value


__all__ = ["ApiModel"]
