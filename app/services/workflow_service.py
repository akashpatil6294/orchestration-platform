"""Workflow drafts, validation, immutable publishing and secrets.

Ownership is enforced here rather than at the route layer so every caller — the
API, the scheduler, the CLI — goes through the same check. Publishing snapshots
the draft and bumps ``latest_version`` atomically, and a version row is never
mutated afterwards.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.core import dag
from app.core.errors import Conflict, Invalid, NotFound
from app.core.security import decrypt_secret, encrypt_secret
from app.models.event import EventType
from app.models.run import WorkflowRun
from app.models.schedule import WorkflowSchedule
from app.models.workflow import Workflow, WorkflowSecret, WorkflowVersion
from app.schemas.workflow import (
    StepDefinition,
    WorkflowCreate,
    WorkflowDefinition,
    WorkflowDraftPatch,
    WorkflowUpdate,
)
from app.services import event_service

DEFAULT_STEP_TIMEOUT = 300


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #
def get_workflow(db: Session, workflow_id: str, owner_id: str) -> Workflow:
    """Fetch a workflow, enforcing ownership or team membership.

    Missing, foreign, and inaccessible look alike (404) to avoid leaking
    which workflows exist.
    """
    from app.services import team_service

    workflow = db.get(Workflow, workflow_id)
    if workflow is None:
        raise NotFound("Workflow not found", code="workflow_not_found")
    if workflow.owner_id == owner_id:
        return workflow
    if workflow.team_id and workflow.team_id in team_service.user_team_ids(db, owner_id):
        return workflow
    raise NotFound("Workflow not found", code="workflow_not_found")


def require_workflow_mutation(db: Session, workflow: Workflow, user_id: str, *, minimum_role: str = "editor") -> None:
    """Raise Forbidden unless the user owns the workflow or holds at least
    ``minimum_role`` on its team. Owners always pass."""
    from app.core.errors import Forbidden
    from app.models.team import role_at_least
    from app.services import team_service

    if workflow.owner_id == user_id:
        return
    if workflow.team_id:
        role = team_service.user_role(db, workflow.team_id, user_id)
        if role and role_at_least(role, minimum_role):
            return
    raise Forbidden("This action requires a higher team role", code="insufficient_role")


def require_workflow_operate(db: Session, workflow: Workflow, user_id: str) -> None:
    """Operators (and above) may start/pause/cancel/retry runs."""
    require_workflow_mutation(db, workflow, user_id, minimum_role="operator")


def list_workflows(
    db: Session,
    owner_id: str,
    *,
    search: str | None = None,
    status: str | None = None,
    include_archived: bool = False,
    limit: int = 25,
    offset: int = 0,
    sort: str = "updated",
) -> tuple[list[Workflow], int]:
    from app.services import team_service

    team_ids = team_service.user_team_ids(db, owner_id)
    query = select(Workflow).where(
        (Workflow.owner_id == owner_id)
        | (Workflow.team_id.in_(team_ids) if team_ids else False)
    )
    if not include_archived:
        query = query.where(Workflow.archived.is_(False))
    if search:
        needle = f"%{search.strip().lower()}%"
        query = query.where(func.lower(Workflow.name).like(needle) | func.lower(Workflow.description).like(needle))
    if status:
        # "status" filters by the most recent run's outcome.
        latest_status = (
            select(WorkflowRun.status)
            .where(WorkflowRun.workflow_id == Workflow.id)
            .order_by(WorkflowRun.created_at.desc())
            .limit(1)
            .scalar_subquery()
        )
        query = query.where(latest_status == status)
    order = {
        "updated": Workflow.updated_at.desc(),
        "created": Workflow.created_at.desc(),
        "name": func.lower(Workflow.name).asc(),
    }.get(sort, Workflow.updated_at.desc())
    total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = list(db.scalars(query.order_by(order).limit(limit).offset(offset)).all())
    return rows, total


def get_version(db: Session, workflow_id: str, version: int) -> WorkflowVersion:
    row = db.scalar(select(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow_id, WorkflowVersion.version == version))
    if row is None:
        raise NotFound("That workflow version does not exist", code="version_not_found")
    return row


def list_versions(db: Session, workflow_id: str, *, limit: int = 50) -> list[WorkflowVersion]:
    return list(
        db.scalars(
            select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow_id)
            .order_by(WorkflowVersion.version.desc())
            .limit(limit)
        ).all()
    )


# --------------------------------------------------------------------------- #
# Draft CRUD
# --------------------------------------------------------------------------- #
def _definition_from_payload(payload: WorkflowCreate | WorkflowUpdate | WorkflowDefinition) -> dict[str, Any]:
    data = payload.model_dump(mode="json")
    for step in data.get("steps", []):
        step.setdefault("name", "")
        step.setdefault("description", "")
        step.setdefault("retries", 0)
        step.setdefault("timeout_seconds", DEFAULT_STEP_TIMEOUT)
        step.setdefault("backoff_seconds", 2)
        step.setdefault("backoff_multiplier", 2.0)
        step.setdefault("required", True)
        step.setdefault("continue_on_error", False)
    data.setdefault("default_max_parallel", 4)
    data.setdefault("tags", [])
    return data


def create_workflow(db: Session, owner_id: str, payload: WorkflowCreate) -> Workflow:
    definition = _definition_from_payload(payload)
    errors = dag.validate_definition(definition)
    if errors:
        raise Invalid("The workflow definition is not valid", code="invalid_workflow", details=errors)
    workflow = Workflow(
        owner_id=owner_id,
        name=payload.name,
        description=payload.description,
        draft=definition,
        default_max_parallel=payload.default_max_parallel,
    )
    db.add(workflow)
    db.flush()
    event_service.emit(
        db,
        EventType.WORKFLOW_CREATED,
        workflow_id=workflow.id,
        message=f"Workflow '{workflow.name}' created",
        payload={"step_count": len(definition["steps"]), "task_types": dag.summarize_definition(definition).get("task_types", [])},
        actor=owner_id,
    )
    return workflow


def update_workflow(db: Session, workflow: Workflow, payload: WorkflowUpdate | WorkflowDraftPatch) -> Workflow:
    data = payload.model_dump(mode="json", exclude_unset=True)
    definition = dict(workflow.draft)
    if "steps" in data and data["steps"] is not None:
        definition["steps"] = data["steps"]
    if "name" in data and data["name"] is not None:
        definition["name"] = data["name"]
        workflow.name = data["name"]
    if "description" in data and data["description"] is not None:
        definition["description"] = data["description"]
        workflow.description = data["description"]
    if "default_max_parallel" in data and data["default_max_parallel"] is not None:
        definition["default_max_parallel"] = data["default_max_parallel"]
        workflow.default_max_parallel = data["default_max_parallel"]
    if "team_id" in data:
        # Assigning to a team requires membership; clearing requires ownership.
        from app.services import team_service

        new_team_id = data["team_id"]
        if new_team_id:
            team_service.require_membership(db, new_team_id, workflow.owner_id)
        workflow.team_id = new_team_id
    workflow.draft = definition
    workflow.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    # Every draft write bumps the optimistic-concurrency counter.
    workflow.draft_version = (workflow.draft_version or 0) + 1
    db.flush()
    return workflow


def archive_workflow(db: Session, workflow: Workflow, *, archived: bool = True) -> Workflow:
    workflow.archived = archived
    db.flush()
    event_service.emit(
        db,
        EventType.WORKFLOW_ARCHIVED,
        workflow_id=workflow.id,
        message=f"Workflow '{workflow.name}' {'archived' if archived else 'restored'}",
        payload={"archived": archived},
    )
    return workflow


def delete_workflow(db: Session, workflow: Workflow) -> None:
    db.delete(workflow)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_definition(definition: dict[str, Any] | WorkflowDefinition | None, *, workflow: Workflow | None = None) -> dict[str, Any]:
    """Validate a definition and return errors, warnings and a summary."""
    if definition is None and workflow is not None:
        definition = workflow.draft
    if definition is None:
        return {"valid": False, "errors": [{"code": "definition.required", "message": "No workflow definition was supplied", "step_id": None, "field": None}], "warnings": [], "summary": {}}
    raw = definition.model_dump(mode="json") if isinstance(definition, WorkflowDefinition) else definition
    errors = dag.validate_definition(raw)
    warnings: list[dict[str, Any]] = []
    summary = dag.summarize_definition(raw)
    if not errors:
        if summary.get("step_count", 0) == 1:
            warnings.append({"code": "graph.single_step", "message": "This workflow has a single step, so nothing runs in parallel", "step_id": None, "field": None})
        if summary.get("max_depth", 0) > 25:
            warnings.append({"code": "graph.deep", "message": "This workflow has a long dependency chain; consider splitting it", "step_id": None, "field": None})
        step_count = summary.get("step_count", 0)
        if step_count > 1 and summary.get("edge_count", 0) == 0:
            warnings.append({"code": "graph.no_edges", "message": "No steps are connected, so all steps start at once", "step_id": None, "field": None})
        for step in raw.get("steps", []):
            if step.get("retries", 0) == 0 and step.get("type", "").endswith((".http", ".request", ".webhook")):
                warnings.append({"code": "step.no_retries", "message": f"Step '{step['id']}' calls an external system but has no retries configured", "step_id": step.get("id"), "field": "retries"})
    return {"valid": not errors, "errors": errors, "warnings": warnings, "summary": summary}


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #
def definition_hash(definition: dict[str, Any]) -> str:
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def publish(db: Session, workflow: Workflow, *, note: str = "", published_by: str | None = None) -> WorkflowVersion:
    result = validate_definition(workflow.draft)
    if not result["valid"]:
        raise Invalid("This workflow cannot be published until the errors are fixed", code="workflow_invalid", details=result["errors"])
    existing = list_versions(db, workflow.id, limit=settings.max_workflow_versions + 1)
    if len(existing) >= settings.max_workflow_versions:
        raise Conflict(
            f"This workflow already has {settings.max_workflow_versions} published versions; delete old runs or create a new workflow",
            code="version_limit_reached",
        )
    next_version = workflow.latest_version + 1
    snapshot = json.loads(json.dumps(workflow.draft))
    version = WorkflowVersion(
        workflow_id=workflow.id,
        version=next_version,
        definition=snapshot,
        definition_hash=definition_hash(snapshot),
        published_by=published_by,
        publish_note=note[:500],
    )
    workflow.latest_version = next_version
    workflow.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(version)
    db.flush()
    event_service.emit(
        db,
        EventType.WORKFLOW_PUBLISHED,
        workflow_id=workflow.id,
        message=f"Published version {next_version}" + (f": {note}" if note else ""),
        payload={"version": next_version, "definition_hash": version.definition_hash, "step_count": len(snapshot.get("steps", []))},
        actor=published_by,
    )
    return version


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #
def set_secret(db: Session, workflow: Workflow, name: str, value: str) -> WorkflowSecret:
    record = db.scalar(select(WorkflowSecret).where(WorkflowSecret.workflow_id == workflow.id, WorkflowSecret.name == name))
    if record is None:
        record = WorkflowSecret(workflow_id=workflow.id, name=name, ciphertext=encrypt_secret(value))
        db.add(record)
    else:
        record.ciphertext = encrypt_secret(value)
        record.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.flush()
    event_service.emit(
        db,
        EventType.WORKFLOW_SECRET_SET,
        workflow_id=workflow.id,
        message=f"Secret '{name}' saved",
        payload={"name": name},
    )
    return record


def delete_secret(db: Session, workflow: Workflow, name: str) -> None:
    record = db.scalar(select(WorkflowSecret).where(WorkflowSecret.workflow_id == workflow.id, WorkflowSecret.name == name))
    if record is None:
        raise NotFound("That secret does not exist", code="secret_not_found")
    db.delete(record)
    event_service.emit(
        db,
        EventType.WORKFLOW_SECRET_DELETED,
        workflow_id=workflow.id,
        message=f"Secret '{name}' removed",
        payload={"name": name},
    )


def list_secrets(db: Session, workflow_id: str) -> list[WorkflowSecret]:
    return list(db.scalars(select(WorkflowSecret).where(WorkflowSecret.workflow_id == workflow_id).order_by(WorkflowSecret.name)).all())


def secret_names(db: Session, workflow_id: str) -> set[str]:
    return {row.name for row in list_secrets(db, workflow_id)}


def referenced_secret_names(definition: dict[str, Any]) -> set[str]:
    """Every secret reference in the definition.

    Two syntaxes are supported: ``{"$secret": "name"}`` object references and
    ``{{secrets.name}}`` templates embedded inside string values.
    """
    import re

    names: set[str] = set()
    template = re.compile(r"\{\{\s*secrets\.([A-Za-z0-9_.-]+?)\s*\}\}")

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if set(node.keys()) == {"$secret"} and isinstance(node["$secret"], str):
                names.add(node["$secret"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            names.update(template.findall(node))

    walk(definition)
    return names


def referenced_connection_names(definition: dict[str, Any]) -> set[str]:
    """Every ``{"$connection": "name"}`` reference in the definition."""
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if set(node.keys()) == {"$connection"} and isinstance(node["$connection"], str):
                names.add(node["$connection"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(definition)
    return names


def resolve_secrets(db: Session, workflow_id: str, definition: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Replace secret references with plaintext, returning redacted key paths.

    Supports ``{"$secret": "name"}`` object references and ``{{secrets.name}}``
    templates inside string values. Called only when building a worker task
    payload — the plaintext is never persisted, logged or returned to a user.
    """
    import re

    template = re.compile(r"\{\{\s*secrets\.([A-Za-z0-9_.-]+?)\s*\}\}")
    resolved_names: list[str] = []

    def _secret_value(name: str, path: str) -> str:
        record = db.scalar(select(WorkflowSecret).where(WorkflowSecret.workflow_id == workflow_id, WorkflowSecret.name == name))
        if record is None:
            raise Invalid(f"Workflow secret '{name}' is not defined", code="secret_missing", details={"secret": name, "path": path})
        try:
            return decrypt_secret(record.ciphertext)
        except ValueError as exc:
            raise Invalid(f"Workflow secret '{name}' could not be decrypted", code="secret_unreadable") from exc

    def walk(node: Any, path: str) -> Any:
        if isinstance(node, dict):
            if set(node.keys()) == {"$secret"}:
                name = node["$secret"]
                value = _secret_value(name, path)
                resolved_names.append(path)
                return value
            return {key: walk(value, f"{path}.{key}" if path else str(key)) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(value, f"{path}[{index}]") for index, value in enumerate(node)]
        if isinstance(node, str) and "secrets." in node and template.search(node):
            value = template.sub(lambda match: _secret_value(match.group(1), path), node)
            resolved_names.append(path)
            return value
        return node

    return walk(definition, ""), resolved_names


def expand_step_inputs(
    db: Session,
    workflow_id: str,
    steps: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve secrets in every step input, returning rewritten step specs."""
    resolved: list[dict[str, Any]] = []
    redacted_paths: list[str] = []
    for step in steps:
        payload, paths = resolve_secrets(db, workflow_id, step.get("input", {}))
        redacted_paths.extend(paths)
        copy = dict(step)
        copy["input"] = payload
        resolved.append(copy)
    return resolved, redacted_paths


def redact_secret_values(db: Session, workflow_id: str, value: Any) -> Any:
    """Redact configured secret values from arbitrary task results and logs."""
    secret_values: list[str] = []
    for record in list_secrets(db, workflow_id):
        try:
            secret = decrypt_secret(record.ciphertext)
        except ValueError:
            continue
        if isinstance(secret, str) and secret:
            secret_values.append(secret)
    secret_values.sort(key=len, reverse=True)

    def scrub(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: scrub(item) for key, item in node.items()}
        if isinstance(node, list):
            return [scrub(item) for item in node]
        if isinstance(node, str):
            result = node
            for secret in secret_values:
                if len(secret) >= 4:
                    result = result.replace(secret, "[REDACTED]")
                elif result == secret:
                    result = "[REDACTED]"
            return result
        return node

    return scrub(value)


# --------------------------------------------------------------------------- #
# Views
# --------------------------------------------------------------------------- #
def run_counts_by_status(db: Session, workflow_ids: list[str]) -> dict[str, dict[str, int]]:
    if not workflow_ids:
        return {}
    rows = db.execute(
        select(WorkflowRun.workflow_id, WorkflowRun.status, func.count())
        .where(WorkflowRun.workflow_id.in_(workflow_ids))
        .group_by(WorkflowRun.workflow_id, WorkflowRun.status)
    ).all()
    result: dict[str, dict[str, int]] = {workflow_id: {} for workflow_id in workflow_ids}
    for workflow_id, status, count in rows:
        result.setdefault(workflow_id, {})[status] = int(count)
    return result


def latest_runs(db: Session, workflow_ids: list[str]) -> dict[str, WorkflowRun]:
    if not workflow_ids:
        return {}
    rows = db.scalars(
        select(WorkflowRun)
        .where(WorkflowRun.workflow_id.in_(workflow_ids))
        .order_by(WorkflowRun.workflow_id, WorkflowRun.created_at.desc())
    ).all()
    result: dict[str, WorkflowRun] = {}
    for row in rows:
        result.setdefault(row.workflow_id, row)
    return result


def schedule_counts(db: Session, workflow_ids: list[str]) -> dict[str, int]:
    if not workflow_ids:
        return {}
    rows = db.execute(
        select(WorkflowSchedule.workflow_id, func.count())
        .where(WorkflowSchedule.workflow_id.in_(workflow_ids), WorkflowSchedule.enabled.is_(True))
        .group_by(WorkflowSchedule.workflow_id)
    ).all()
    return {workflow_id: int(count) for workflow_id, count in rows}


def draft_changed(workflow: Workflow) -> bool:
    """True when the draft differs from the newest published version."""
    if workflow.latest_version == 0:
        return True
    version = workflow.versions[-1] if workflow.versions else None
    if version is None:
        return True
    return definition_hash(workflow.draft) != version.definition_hash


def diff_definitions(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Structural diff between two workflow definitions (Stage H1).

    Returns steps added/removed/changed (with per-field changes), edge
    (depends_on) changes, and workflow-level field changes. Used by the
    builder's publish flow to show what publishing will change.
    """
    def _steps(definition: dict[str, Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for step in definition.get("steps", []) or []:
            if isinstance(step, dict) and step.get("id"):
                result[str(step["id"])] = step
        return result

    old_steps = _steps(old)
    new_steps = _steps(new)
    added = sorted(set(new_steps) - set(old_steps))
    removed = sorted(set(old_steps) - set(new_steps))
    changed: list[dict[str, Any]] = []
    edge_changes: list[dict[str, Any]] = []
    for step_id in sorted(set(old_steps) & set(new_steps)):
        old_step = old_steps[step_id]
        new_step = new_steps[step_id]
        fields = sorted(set(old_step) | set(new_step))
        field_changes = [
            {"field": field, "old": old_step.get(field), "new": new_step.get(field)}
            for field in fields
            if old_step.get(field) != new_step.get(field)
        ]
        if field_changes:
            changed.append({"step_id": step_id, "fields": field_changes})
        old_deps = set(old_step.get("depends_on") or [])
        new_deps = set(new_step.get("depends_on") or [])
        if old_deps != new_deps:
            edge_changes.append(
                {"step_id": step_id, "removed_edges": sorted(old_deps - new_deps), "added_edges": sorted(new_deps - old_deps)}
            )
    workflow_fields = sorted(set(old) | set(new) - {"steps"})
    workflow_changes = [
        {"field": field, "old": old.get(field), "new": new.get(field)}
        for field in workflow_fields
        if old.get(field) != new.get(field)
    ]
    return {
        "steps_added": added,
        "steps_removed": removed,
        "steps_changed": changed,
        "edge_changes": edge_changes,
        "workflow_changes": workflow_changes,
        "has_changes": bool(added or removed or changed or workflow_changes),
    }


def summarize(db: Session, workflow: Workflow, *, counts: dict[str, int] | None = None, last_run: WorkflowRun | None = None, schedules: int = 0) -> dict[str, Any]:
    summary = dag.summarize_definition(workflow.draft) if workflow.draft else {"step_count": 0}
    return {
        "id": workflow.id,
        "name": workflow.name,
        "description": workflow.description,
        "latest_version": workflow.latest_version,
        "step_count": summary.get("step_count", 0),
        "archived": workflow.archived,
        "default_max_parallel": workflow.default_max_parallel,
        "created_at": workflow.created_at,
        "updated_at": workflow.updated_at,
        "run_counts": counts or {},
        "last_run_at": last_run.created_at if last_run else None,
        "last_run_status": last_run.status if last_run else None,
        "has_draft_changes": draft_changed(workflow),
        "schedule_count": schedules,
        "draft_version": workflow.draft_version or 1,
    }


def step_specs(definition: dict[str, Any]) -> list[StepDefinition]:
    return [StepDefinition.model_validate(step) for step in definition.get("steps", [])]


__all__ = [
    "DEFAULT_STEP_TIMEOUT",
    "archive_workflow",
    "create_workflow",
    "definition_hash",
    "delete_secret",
    "delete_workflow",
    "draft_changed",
    "expand_step_inputs",
    "referenced_connection_names",
    "get_version",
    "get_workflow",
    "latest_runs",
    "list_secrets",
    "list_versions",
    "list_workflows",
    "publish",
    "referenced_secret_names",
    "resolve_secrets",
    "redact_secret_values",
    "run_counts_by_status",
    "schedule_counts",
    "secret_names",
    "set_secret",
    "step_specs",
    "summarize",
    "update_workflow",
    "validate_definition",
]
