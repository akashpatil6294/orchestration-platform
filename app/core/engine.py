"""Pure helpers for the workflow execution engine.

Kept free of the database so join rules, condition skips and fan-out keys can be
unit tested without spinning up a run.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

JOIN_ALL_SUCCESS = "all_success"
JOIN_ANY_SUCCESS = "any_success"
JOIN_ALL_DONE = "all_done"
JOIN_MODES = (JOIN_ALL_SUCCESS, JOIN_ANY_SUCCESS, JOIN_ALL_DONE)

PARTIAL_FAIL_FAST = "fail_fast"
PARTIAL_CONTINUE = "continue"
PARTIAL_FAILURE_POLICIES = (PARTIAL_FAIL_FAST, PARTIAL_CONTINUE)

SKIP_CONDITION = "condition"
SKIP_DEPENDENCY = "dependency_failed"
SKIP_OPTIONAL = "optional_skip"

SYSTEM_TASK_TYPES = frozenset({"approval", "workflow.run", "condition"})

WAITING_APPROVAL = "waiting_approval"
WAITING_CHILDREN = "waiting_children"
WAITING_SUBWORKFLOW = "waiting_subworkflow"

ACTIVE_ENGINE_STATUSES = {
    "pending",
    "running",
    "retrying",
    WAITING_APPROVAL,
    WAITING_CHILDREN,
    WAITING_SUBWORKFLOW,
    "waiting_compensation",
}
TERMINAL_ENGINE_STATUSES = {"succeeded", "failed", "cancelled", "skipped"}
BLOCKING_TERMINAL = {"failed", "cancelled"}

FOREACH_SEP = "__i"


def foreach_child_key(parent_key: str, index: int) -> str:
    return f"{parent_key}{FOREACH_SEP}{index}"


def is_foreach_child_key(step_key: str, parent_key: str) -> bool:
    prefix = f"{parent_key}{FOREACH_SEP}"
    return step_key.startswith(prefix) and step_key[len(prefix) :].isdigit()


def foreach_index_from_key(step_key: str, parent_key: str) -> int | None:
    prefix = f"{parent_key}{FOREACH_SEP}"
    if not step_key.startswith(prefix):
        return None
    tail = step_key[len(prefix) :]
    return int(tail) if tail.isdigit() else None


def spec_of(step: Mapping[str, Any]) -> dict[str, Any]:
    spec = _field(step, "spec") or _field(step, "spec_json")
    if isinstance(spec, dict):
        return spec
    return {}


def join_mode(step: Mapping[str, Any]) -> str:
    mode = spec_of(step).get("join") or _field(step, "join") or JOIN_ALL_SUCCESS
    return mode if mode in JOIN_MODES else JOIN_ALL_SUCCESS


def skip_reason(step: Mapping[str, Any]) -> str | None:
    reason = _field(step, "skip_reason")
    if reason:
        return str(reason)
    error = _field(step, "error")
    if isinstance(error, dict) and error.get("code") == "condition_false":
        return SKIP_CONDITION
    return None


def is_condition_skip(step: Mapping[str, Any]) -> bool:
    return _field(step, "status") == "skipped" and skip_reason(step) == SKIP_CONDITION


def is_optional_outcome(step: Mapping[str, Any]) -> bool:
    """A dependency outcome that should not block descendants by itself."""
    if is_condition_skip(step):
        return True
    status = _field(step, "status")
    if status in {"failed", "skipped"} and spec_of(step).get("continue_on_error"):
        return True
    return False


def join_decision(deps: Iterable[Mapping[str, Any]], *, mode: str) -> str:
    """Return ``ready``, ``wait`` or ``skip`` for a step given its dependencies."""
    items = list(deps)
    if not items:
        return "ready"
    statuses = [_field(item, "status") for item in items]
    if any(status in ACTIVE_ENGINE_STATUSES or status not in TERMINAL_ENGINE_STATUSES for status in statuses):
        if mode == JOIN_ANY_SUCCESS and any(status == "succeeded" for status in statuses):
            return "ready"
        return "wait"

    if mode == JOIN_ALL_DONE:
        return "ready"
    if mode == JOIN_ANY_SUCCESS:
        return "ready" if any(_field(item, "status") == "succeeded" for item in items) else "skip"

    # all_success: ignore optional/condition skips, require the rest succeeded.
    blockers = [
        item
        for item in items
        if _field(item, "status") != "succeeded" and not is_optional_outcome(item)
    ]
    if blockers:
        return "skip"
    remaining = [item for item in items if not is_optional_outcome(item)]
    if remaining and all(_field(item, "status") == "succeeded" for item in remaining):
        return "ready"
    if not remaining:
        # Every dependency was an optional skip; still allow the step to run.
        return "ready"
    return "skip"


def _field(item: Any, name: str, default: Any = None) -> Any:
    """Read a status shape from either a mapping or an ORM row."""
    if isinstance(item, Mapping):
        return item.get(name, default)
    if name == "spec" and hasattr(item, "spec_json"):
        return getattr(item, "spec_json") or default
    return getattr(item, name, default)


def cache_key_material(task_type: str, resolved_input: Any, explicit_key: str | None = None) -> str:
    import hashlib
    import json

    material = {"type": task_type, "key": explicit_key} if explicit_key and explicit_key != "input-hash" else {
        "type": task_type,
        "input": resolved_input,
    }
    payload = json.dumps(material, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "ACTIVE_ENGINE_STATUSES",
    "BLOCKING_TERMINAL",
    "FOREACH_SEP",
    "JOIN_ALL_DONE",
    "JOIN_ALL_SUCCESS",
    "JOIN_ANY_SUCCESS",
    "JOIN_MODES",
    "PARTIAL_CONTINUE",
    "PARTIAL_FAIL_FAST",
    "PARTIAL_FAILURE_POLICIES",
    "SKIP_CONDITION",
    "SKIP_DEPENDENCY",
    "SYSTEM_TASK_TYPES",
    "TERMINAL_ENGINE_STATUSES",
    "WAITING_APPROVAL",
    "WAITING_CHILDREN",
    "WAITING_SUBWORKFLOW",
    "cache_key_material",
    "foreach_child_key",
    "foreach_index_from_key",
    "is_condition_skip",
    "is_foreach_child_key",
    "is_optional_outcome",
    "join_decision",
    "join_mode",
    "skip_reason",
    "spec_of",
]
