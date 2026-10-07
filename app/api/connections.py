"""Reusable connections: named credentials shared across workflows.

Values are write-only: the API never returns a connection's plaintext value.
Workers receive it only inside task payloads that reference the connection.
"""
from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.models.connection import CONNECTION_KINDS
from app.services import connection_service

router = APIRouter(prefix="/api/v1/connections", tags=["connections"])


class ConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(default="generic")
    value: str = Field(min_length=1, max_length=4000)
    team_id: str | None = None


class ConnectionUpdate(BaseModel):
    value: str = Field(min_length=1, max_length=4000)
    team_id: str | None = None


@router.get("")
def list_connections(user: CurrentUser, db: DbSession) -> dict:
    """The user's own connections plus connections shared with their teams."""
    connections = connection_service.list_for_user(db, user.id)
    return {"items": [connection_service.connection_view(item) for item in connections]}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_connection(payload: ConnectionCreate, user: CurrentUser, db: DbSession) -> dict:
    connection = connection_service.create_connection(
        db,
        owner_id=user.id,
        name=payload.name,
        kind=payload.kind,
        value=payload.value,
        team_id=payload.team_id,
    )
    return connection_service.connection_view(connection)


@router.get("/kinds")
def connection_kinds() -> dict:
    return {"kinds": list(CONNECTION_KINDS)}


@router.patch("/{connection_id}")
def update_connection(connection_id: str, payload: ConnectionUpdate, user: CurrentUser, db: DbSession) -> dict:
    connection = connection_service.get_for_user(db, connection_id, user.id)
    if connection.owner_id != user.id:
        from app.core.errors import Forbidden

        raise Forbidden("Only the connection owner can change its value", code="not_owner")
    updated = connection_service.update_connection(db, connection, value=payload.value, team_id=payload.team_id)
    return connection_service.connection_view(updated)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(connection_id: str, user: CurrentUser, db: DbSession) -> None:
    connection = connection_service.get_for_user(db, connection_id, user.id)
    if connection.owner_id != user.id:
        from app.core.errors import Forbidden

        raise Forbidden("Only the connection owner can delete it", code="not_owner")
    connection_service.delete_connection(db, connection)


__all__ = ["router"]
