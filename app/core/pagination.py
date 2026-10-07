"""Pagination, filtering and request-size helpers shared by the API routers."""
from __future__ import annotations

import json
from typing import Any, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.errors import PayloadTooLarge

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
    has_more: bool = False


class Pagination(BaseModel):
    limit: int = Field(default=25, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


def pagination(
    limit: int = Query(25, ge=1, le=200, description="Maximum rows to return"),
    offset: int = Query(0, ge=0, description="Rows to skip"),
) -> Pagination:
    return Pagination(limit=limit, offset=offset)


def paginate(db: Session, query: Select[Any], page: Pagination, *, count_query: Select[Any] | None = None) -> tuple[list[Any], int]:
    total = db.scalar(count_query if count_query is not None else select(func.count()).select_from(query.subquery())) or 0
    rows = list(db.scalars(query.limit(page.limit).offset(page.offset)).all())
    return rows, int(total)


def page_of(items: list[Any], total: int, page: Pagination) -> dict[str, Any]:
    return {
        "items": items,
        "total": total,
        "limit": page.limit,
        "offset": page.offset,
        "has_more": page.offset + len(items) < total,
    }


def enforce_payload_size(value: Any, limit: int, *, label: str) -> None:
    """Reject oversized JSON bodies before they reach the database."""
    try:
        size = len(json.dumps(value, default=str).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise PayloadTooLarge(f"{label} must be JSON-serialisable") from exc
    if size > limit:
        raise PayloadTooLarge(f"{label} is {size} bytes; the limit is {limit} bytes", details={"bytes": size, "limit": limit})


def check_run_input(value: Any) -> None:
    enforce_payload_size(value, settings.max_run_input_bytes, label="Run input")


def check_task_output(value: Any) -> None:
    enforce_payload_size(value, settings.max_task_output_bytes, label="Task output")


__all__ = [
    "Page",
    "Pagination",
    "check_run_input",
    "check_task_output",
    "enforce_payload_size",
    "page_of",
    "paginate",
    "pagination",
]
