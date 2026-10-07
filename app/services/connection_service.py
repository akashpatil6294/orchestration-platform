"""CRUD and dispatch-time resolution for reusable team connections."""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, Invalid, NotFound
from app.core.security import decrypt_secret, encrypt_secret
from app.models.base import utcnow
from app.models.connection import CONNECTION_KINDS, Connection
from app.models.team import TeamMembership
from app.models.workflow import Workflow

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_PATTERN.match(name):
        raise Invalid("Connection name must be 1-120 chars of letters, digits, '_', '-', '.'", code="invalid_name")
    return name


def _validate_kind(kind: str) -> str:
    if kind not in CONNECTION_KINDS:
        raise Invalid(f"Unknown connection kind '{kind}'", code="invalid_kind", details={"kinds": list(CONNECTION_KINDS)})
    return kind


def _validate_value(kind: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise Invalid("Connection value must be 1-4000 characters", code="invalid_value")
    value = value.strip()
    if kind == "slack_webhook" and not value.startswith("https://hooks.slack.com/"):
        raise Invalid("Slack webhook must start with https://hooks.slack.com/", code="invalid_value")
    if kind == "sql_url" and "://" not in value:
        raise Invalid("SQL connection URL must contain '://'", code="invalid_value")
    return value


def _team_ids_for(db: Session, user_id: str) -> set[str]:
    return set(
        db.scalars(select(TeamMembership.team_id).where(TeamMembership.user_id == user_id)).all()
    )


def create_connection(
    db: Session, *, owner_id: str, name: str, kind: str, value: str, team_id: str | None = None
) -> Connection:
    name = _validate_name(name)
    kind = _validate_kind(kind)
    value = _validate_value(kind, value)
    if team_id is not None and team_id not in _team_ids_for(db, owner_id):
        raise NotFound("Team not found", code="team_not_found")
    existing = db.scalar(select(Connection).where(Connection.owner_id == owner_id, Connection.name == name))
    if existing is not None:
        raise Conflict(f"Connection '{name}' already exists", code="connection_exists")
    connection = Connection(
        owner_id=owner_id,
        team_id=team_id,
        name=name,
        kind=kind,
        value_ciphertext=encrypt_secret(value),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return connection


def update_connection(db: Session, connection: Connection, *, value: str, team_id: str | None = None) -> Connection:
    value = _validate_value(connection.kind, value)
    if team_id is not None and team_id not in _team_ids_for(db, connection.owner_id):
        raise NotFound("Team not found", code="team_not_found")
    connection.value_ciphertext = encrypt_secret(value)
    connection.team_id = team_id
    connection.updated_at = utcnow()
    db.commit()
    db.refresh(connection)
    return connection


def delete_connection(db: Session, connection: Connection) -> None:
    db.delete(connection)
    db.commit()


def get_for_user(db: Session, connection_id: str, user_id: str) -> Connection:
    """Fetch by ID if the user owns it or belongs to its team."""
    connection = db.get(Connection, connection_id)
    if connection is None:
        raise NotFound("Connection not found", code="connection_not_found")
    if connection.owner_id != user_id and (
        connection.team_id is None or connection.team_id not in _team_ids_for(db, user_id)
    ):
        raise NotFound("Connection not found", code="connection_not_found")
    return connection


def list_for_user(db: Session, user_id: str) -> list[Connection]:
    team_ids = _team_ids_for(db, user_id)
    query = select(Connection).where(Connection.owner_id == user_id)
    if team_ids:
        query = select(Connection).where(
            (Connection.owner_id == user_id) | (Connection.team_id.in_(team_ids))
        )
    return list(db.scalars(query.order_by(Connection.name)).all())


def connection_view(connection: Connection) -> dict[str, Any]:
    # The value is never returned — only metadata. The value is revealed solely
    # to workers at dispatch time via {"$connection": name} resolution.
    return {
        "id": connection.id,
        "name": connection.name,
        "kind": connection.kind,
        "team_id": connection.team_id,
        "owner_id": connection.owner_id,
        "created_at": connection.created_at,
        "updated_at": connection.updated_at,
    }


def resolve_connection_value(db: Session, *, owner_id: str, team_id: str | None, name: str) -> str:
    """Decrypt a connection value for dispatch. Owner's own connection wins;
    team-shared connections are the fallback."""
    record = db.scalar(select(Connection).where(Connection.owner_id == owner_id, Connection.name == name))
    if record is None and team_id is not None:
        record = db.scalar(
            select(Connection).where(Connection.team_id == team_id, Connection.name == name)
        )
    if record is None:
        raise Invalid(f"Connection '{name}' is not defined", code="connection_missing", details={"connection": name})
    try:
        return decrypt_secret(record.value_ciphertext)
    except ValueError as exc:
        raise Invalid(f"Connection '{name}' could not be decrypted", code="connection_unreadable") from exc


def resolve_connections(
    db: Session, workflow_id: str, definition: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Replace ``{"$connection": name}`` references with plaintext.

    Mirrors ``workflow_service.resolve_secrets``: called only when building a
    worker task payload. Team scope comes from the workflow's team.
    """
    workflow = db.get(Workflow, workflow_id)
    owner_id = workflow.owner_id if workflow else ""
    team_id = workflow.team_id if workflow else None
    resolved_paths: list[str] = []

    def walk(node: Any, path: str) -> Any:
        if isinstance(node, dict):
            if set(node.keys()) == {"$connection"}:
                name = node["$connection"]
                if not isinstance(name, str):
                    raise Invalid("Connection reference must name a connection", code="invalid_connection_ref")
                value = resolve_connection_value(db, owner_id=owner_id, team_id=team_id, name=name)
                resolved_paths.append(path)
                return value
            return {key: walk(value, f"{path}.{key}" if path else str(key)) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(value, f"{path}[{index}]") for index, value in enumerate(node)]
        return node

    return walk(definition, ""), resolved_paths


__all__ = [
    "connection_view",
    "create_connection",
    "delete_connection",
    "get_for_user",
    "list_for_user",
    "resolve_connection_value",
    "resolve_connections",
    "update_connection",
]
