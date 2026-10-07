"""Saved run filters: named filter sets stored server-side per user."""
from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.errors import NotFound
from app.models.saved_filter import SavedFilter

router = APIRouter(prefix="/api/v1/saved-filters", tags=["saved-filters"])

_ALLOWED_KEYS = {"status", "workflow_id", "trigger", "sort", "search"}


class SavedFilterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    filters: dict = Field(default_factory=dict)


def _view(item: SavedFilter) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "filters": item.filters,
        "created_at": item.created_at,
    }


@router.get("")
def list_saved_filters(user: CurrentUser, db: DbSession) -> dict:
    items = db.scalars(
        select(SavedFilter).where(SavedFilter.user_id == user.id).order_by(SavedFilter.created_at.desc())
    ).all()
    return {"items": [_view(item) for item in items]}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_saved_filter(payload: SavedFilterCreate, user: CurrentUser, db: DbSession) -> dict:
    from app.core.errors import Invalid

    unknown = set(payload.filters) - _ALLOWED_KEYS
    if unknown:
        raise Invalid(f"Unknown filter keys: {sorted(unknown)}", code="invalid_filter")
    item = SavedFilter(user_id=user.id, name=payload.name.strip(), filters=dict(payload.filters))
    db.add(item)
    db.commit()
    db.refresh(item)
    return _view(item)


@router.delete("/{filter_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_saved_filter(filter_id: str, user: CurrentUser, db: DbSession) -> None:
    item = db.get(SavedFilter, filter_id)
    if item is None or item.user_id != user.id:
        raise NotFound("Saved filter not found", code="filter_not_found")
    db.delete(item)
    db.commit()


__all__ = ["router"]
