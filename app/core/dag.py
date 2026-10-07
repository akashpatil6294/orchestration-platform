"""Workflow definition validation.

This module owns the workflow *contract*: what a valid step looks like, how big
a workflow may be, and which shapes of graph are legal. It is pure (no database,
no network) so it can be unit tested and reused by the API, the CLI and the
frontend validation endpoint.

Definitions are validated defensively: unknown fields are rejected, and nothing
here ever evaluates user-supplied code.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Iterable

from app.config import settings
from app.core.dataflow import validate_templates
from app.core.engine import JOIN_MODES, PARTIAL_FAILURE_POLICIES, SYSTEM_TASK_TYPES
from app.core.expr import validate_expression

STEP_ID_PATTERN = r"^[A-Za-z0-9_.-]+$"
TASK_TYPE_PATTERN = r"^[A-Za-z0-9_.:-]+$"

# Reserved prefixes stop a workflow author from shadowing platform internals.
RESERVED_TASK_PREFIXES = ("orchestrator.", "system.", "internal.")

ALLOWED_INPUT_KEYS = {
    "input",
    "depends_on",
    "retries",
    "timeout_seconds",
    "backoff_seconds",
    "backoff_multiplier",
    "retry_max_delay_seconds",
    "retry_jitter",
    "description",
    "name",
    "required",
    "continue_on_error",
    "position",
    "when",
    "join",
    "foreach",
    "max_concurrency",
    "partial_failure",
    "cache",
    "approvers",
    "on_timeout",
    "compensate",
    "priority",
    "queue",
    "concurrency_key",
    "concurrency_limit",
    "rate_limit_per_minute",
    "rate_limit_key",
    "if_true",
    "if_false",
}
REQUIRED_STEP_KEYS = {"id", "type"}
ALLOWED_DEFINITION_KEYS = {
    "name",
    "description",
    "steps",
    "default_max_parallel",
    "tags",
    "on_failure",
    "timeout_seconds",
    "sla_seconds",
}

MAX_RETRIES = 20
MAX_TIMEOUT_SECONDS = 86_400
MAX_INPUT_BYTES = 65_536
MAX_STEP_DEPTH = 60


class ValidationIssue(dict):
    """A single validation problem, shaped for the API response."""

    def __init__(self, code: str, message: str, step_id: str | None = None, field: str | None = None) -> None:
        super().__init__(code=code, message=message, step_id=step_id, field=field)


def _issue(code: str, message: str, step_id: str | None = None, field: str | None = None) -> ValidationIssue:
    return ValidationIssue(code, message, step_id, field)


def _approx_bytes(value: Any) -> int:
    import json

    try:
        return len(json.dumps(value, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return MAX_INPUT_BYTES + 1


def validate_definition(definition: dict[str, Any], *, max_steps: int | None = None) -> list[dict[str, Any]]:
    """Validate a workflow definition and return a list of issues (empty = valid)."""
    limit = max_steps or settings.max_workflow_steps
    issues: list[dict[str, Any]] = []

    if not isinstance(definition, dict):
        return [_issue("definition.invalid", "Workflow definition must be an object")]

    name = definition.get("name")
    if not isinstance(name, str) or not name.strip():
        issues.append(_issue("name.required", "Workflow name is required", field="name"))
    elif len(name) > 200:
        issues.append(_issue("name.too_long", "Workflow name must be 200 characters or fewer", field="name"))

    description = definition.get("description", "")
    if not isinstance(description, str):
        issues.append(_issue("description.invalid", "Description must be text", field="description"))
    elif len(description) > 4000:
        issues.append(_issue("description.too_long", "Description must be 4000 characters or fewer", field="description"))

    timeout = definition.get("timeout_seconds")
    if timeout is not None and (not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT_SECONDS):
        issues.append(_issue("timeout.invalid", "Workflow timeout_seconds must be between 1 and 86400", field="timeout_seconds"))
    sla = definition.get("sla_seconds")
    if sla is not None and (not isinstance(sla, int) or isinstance(sla, bool) or not 1 <= sla <= MAX_TIMEOUT_SECONDS):
        issues.append(_issue("sla.invalid", "Workflow sla_seconds must be between 1 and 86400", field="sla_seconds"))

    on_failure = definition.get("on_failure", [])
    if on_failure is None:
        on_failure = []
    if not isinstance(on_failure, list):
        issues.append(_issue("on_failure.invalid", "on_failure must be a list of steps", field="on_failure"))
        on_failure = []

    steps = definition.get("steps")
    if not isinstance(steps, list) or not steps:
        issues.append(_issue("steps.required", "A workflow needs at least one step", field="steps"))
        return issues
    graph_steps = list(steps) + [item for item in on_failure if isinstance(item, dict)]
    if len(steps) > limit:
        issues.append(_issue("steps.too_many", f"A workflow may have at most {limit} steps", field="steps"))
    if len(graph_steps) > limit:
        issues.append(_issue("steps.too_many", f"A workflow may have at most {limit} steps including on_failure", field="steps"))

    known_ids: set[str] = set()
    duplicates: set[str] = set()
    for index, step in enumerate(graph_steps):
        field_name = "on_failure" if index >= len(steps) else "steps"
        if not isinstance(step, dict):
            issues.append(_issue("step.invalid", f"Step {index + 1} must be an object", field=field_name))
            continue
        step_id = step.get("id")
        if not isinstance(step_id, str) or not step_id:
            issues.append(_issue("step.id_required", f"Step {index + 1} is missing an id", field="id"))
            continue
        if not _matches(step_id, STEP_ID_PATTERN):
            issues.append(_issue("step.id_invalid", f"Step id '{step_id}' may only contain letters, numbers, dot, dash and underscore", step_id, "id"))
        if len(step_id) > 128:
            issues.append(_issue("step.id_too_long", f"Step id '{step_id[:24]}...' is longer than 128 characters", step_id, "id"))
        if step_id in known_ids:
            duplicates.add(step_id)
        known_ids.add(step_id)

    if duplicates:
        for step_id in sorted(duplicates):
            issues.append(_issue("step.id_duplicate", f"Step id '{step_id}' is used more than once", step_id, "id"))

    steps_by_id = {
        step["id"]: step
        for step in graph_steps
        if isinstance(step, dict) and isinstance(step.get("id"), str)
    }

    # Second pass: per-step fields and dependency references.
    edges: dict[str, list[str]] = {}
    for step in graph_steps:
        if not isinstance(step, dict):
            continue
        step_id = step.get("id")
        if not isinstance(step_id, str) or not step_id:
            continue

        unknown = set(step.keys()) - ALLOWED_INPUT_KEYS - REQUIRED_STEP_KEYS
        if unknown:
            issues.append(_issue("step.unknown_field", f"Step '{step_id}' has unsupported field(s): {', '.join(sorted(unknown))}", step_id))

        task_type = step.get("type")
        if not isinstance(task_type, str) or not task_type.strip():
            issues.append(_issue("step.type_required", f"Step '{step_id}' needs a task type", step_id, "type"))
        else:
            if not _matches(task_type, TASK_TYPE_PATTERN):
                issues.append(_issue("step.type_invalid", f"Task type '{task_type}' contains unsupported characters", step_id, "type"))
            if task_type.startswith(RESERVED_TASK_PREFIXES):
                issues.append(_issue("step.type_reserved", f"Task type '{task_type}' uses a reserved prefix", step_id, "type"))
            if len(task_type) > 200:
                issues.append(_issue("step.type_too_long", f"Task type for step '{step_id}' is too long", step_id, "type"))

        payload = step.get("input", {})
        if not isinstance(payload, dict):
            issues.append(_issue("step.input_invalid", f"Step '{step_id}' input must be a JSON object", step_id, "input"))
        elif _approx_bytes(payload) > MAX_INPUT_BYTES:
            issues.append(_issue("step.input_too_large", f"Step '{step_id}' input exceeds {MAX_INPUT_BYTES // 1024} KB", step_id, "input"))

        retries = step.get("retries", 0)
        if not isinstance(retries, int) or isinstance(retries, bool) or not 0 <= retries <= MAX_RETRIES:
            issues.append(_issue("step.retries_invalid", f"Step '{step_id}' retries must be between 0 and {MAX_RETRIES}", step_id, "retries"))

        timeout = step.get("timeout_seconds", 300)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
            issues.append(_issue("step.timeout_invalid", f"Step '{step_id}' timeout must be between 1 and {MAX_TIMEOUT_SECONDS} seconds", step_id, "timeout_seconds"))

        backoff = step.get("backoff_seconds", 2)
        if not isinstance(backoff, int) or isinstance(backoff, bool) or not 0 <= backoff <= 3600:
            issues.append(_issue("step.backoff_invalid", f"Step '{step_id}' backoff must be between 0 and 3600 seconds", step_id, "backoff_seconds"))

        multiplier = step.get("backoff_multiplier", 2.0)
        if not isinstance(multiplier, (int, float)) or isinstance(multiplier, bool) or not 1.0 <= float(multiplier) <= 10.0:
            issues.append(_issue("step.backoff_multiplier_invalid", f"Step '{step_id}' backoff multiplier must be between 1 and 10", step_id, "backoff_multiplier"))

        retry_cap = step.get("retry_max_delay_seconds")
        if retry_cap is not None and (not isinstance(retry_cap, int) or isinstance(retry_cap, bool) or not 1 <= retry_cap <= 86_400):
            issues.append(_issue("step.retry_max_delay_invalid", f"Step '{step_id}' retry_max_delay_seconds must be between 1 and 86400", step_id, "retry_max_delay_seconds"))
        retry_jitter = step.get("retry_jitter")
        if retry_jitter is not None and (not isinstance(retry_jitter, (int, float)) or isinstance(retry_jitter, bool) or not 0 <= float(retry_jitter) <= 1):
            issues.append(_issue("step.retry_jitter_invalid", f"Step '{step_id}' retry_jitter must be between 0 and 1", step_id, "retry_jitter"))

        priority = step.get("priority", 0)
        if not isinstance(priority, int) or isinstance(priority, bool) or not -1000 <= priority <= 1000:
            issues.append(_issue("step.priority_invalid", f"Step '{step_id}' priority must be between -1000 and 1000", step_id, "priority"))
        queue_name = step.get("queue", "default")
        if not isinstance(queue_name, str) or not queue_name or len(queue_name) > 120 or not _matches(queue_name, r"^[A-Za-z0-9_.:-]+$"):
            issues.append(_issue("step.queue_invalid", f"Step '{step_id}' queue must be a 1-120 character queue name", step_id, "queue"))
        concurrency_limit = step.get("concurrency_limit")
        if concurrency_limit is not None and (not isinstance(concurrency_limit, int) or isinstance(concurrency_limit, bool) or not 1 <= concurrency_limit <= 10000):
            issues.append(_issue("step.concurrency_limit_invalid", f"Step '{step_id}' concurrency_limit must be between 1 and 10000", step_id, "concurrency_limit"))
        rate_limit = step.get("rate_limit_per_minute")
        if rate_limit is not None and (not isinstance(rate_limit, int) or isinstance(rate_limit, bool) or not 1 <= rate_limit <= 1_000_000):
            issues.append(_issue("step.rate_limit_invalid", f"Step '{step_id}' rate_limit_per_minute must be between 1 and 1000000", step_id, "rate_limit_per_minute"))

        required = step.get("required", True)
        if not isinstance(required, bool):
            issues.append(_issue("step.required_invalid", f"Step '{step_id}' required must be a boolean", step_id, "required"))

        deps = step.get("depends_on", [])
        if not isinstance(deps, list) or not all(isinstance(item, str) for item in deps):
            issues.append(_issue("step.depends_on_invalid", f"Step '{step_id}' dependencies must be a list of step ids", step_id, "depends_on"))
            deps = []
        elif len(deps) != len(set(deps)):
            issues.append(_issue("step.depends_on_duplicate", f"Step '{step_id}' lists the same dependency twice", step_id, "depends_on"))
        for dep in deps:
            if dep == step_id:
                issues.append(_issue("step.depends_on_self", f"Step '{step_id}' cannot depend on itself", step_id, "depends_on"))
            elif dep not in known_ids:
                issues.append(_issue("step.depends_on_unknown", f"Step '{step_id}' depends on unknown step '{dep}'", step_id, "depends_on"))
        edges[step_id] = [dep for dep in deps if dep in known_ids and dep != step_id]
        for message in validate_templates(
            payload,
            step_id=step_id,
            dependencies=set(deps),
            allow_foreach=bool(step.get("foreach")),
        ):
            issues.append(_issue("step.input_reference_invalid", message, step_id, "input"))

        when = step.get("when")
        if when is not None:
            if not isinstance(when, str):
                issues.append(_issue("step.when_invalid", f"Step '{step_id}' when must be a condition string", step_id, "when"))
            else:
                for message in validate_expression(when, step_id=step_id, dependencies=set(deps)):
                    issues.append(_issue("step.when_invalid", message, step_id, "when"))

        join = step.get("join", "all_success")
        if join is not None and join not in JOIN_MODES:
            issues.append(
                _issue("step.join_invalid", f"Step '{step_id}' join must be one of {', '.join(JOIN_MODES)}", step_id, "join")
            )

        foreach = step.get("foreach")
        if foreach is not None:
            if not isinstance(foreach, str) or not foreach.strip():
                issues.append(_issue("step.foreach_invalid", f"Step '{step_id}' foreach must be a data reference string", step_id, "foreach"))
            else:
                for message in validate_templates(foreach, step_id=step_id, dependencies=set(deps)):
                    issues.append(_issue("step.foreach_invalid", message, step_id, "foreach"))

        max_concurrency = step.get("max_concurrency")
        if max_concurrency is not None and (
            not isinstance(max_concurrency, int) or isinstance(max_concurrency, bool) or not 1 <= max_concurrency <= 256
        ):
            issues.append(_issue("step.max_concurrency_invalid", f"Step '{step_id}' max_concurrency must be between 1 and 256", step_id, "max_concurrency"))

        partial = step.get("partial_failure", "fail_fast")
        if partial is not None and partial not in PARTIAL_FAILURE_POLICIES:
            issues.append(
                _issue(
                    "step.partial_failure_invalid",
                    f"Step '{step_id}' partial_failure must be fail_fast or continue",
                    step_id,
                    "partial_failure",
                )
            )

        cache = step.get("cache")
        if cache is not None:
            if not isinstance(cache, dict):
                issues.append(_issue("step.cache_invalid", f"Step '{step_id}' cache must be an object", step_id, "cache"))
            else:
                ttl = cache.get("ttl_seconds", 0)
                key = cache.get("key", "input-hash")
                if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 30 * 24 * 3600:
                    issues.append(_issue("step.cache_invalid", f"Step '{step_id}' cache.ttl_seconds must be between 1 and 30 days", step_id, "cache"))
                if key is not None and not isinstance(key, str):
                    issues.append(_issue("step.cache_invalid", f"Step '{step_id}' cache.key must be text", step_id, "cache"))

        if step.get("type") == "approval":
            approvers = step.get("approvers", [])
            if approvers is None:
                approvers = []
            if not isinstance(approvers, list) or not all(isinstance(item, str) and item.strip() for item in approvers):
                issues.append(_issue("step.approvers_invalid", f"Step '{step_id}' approvers must be a list of identities", step_id, "approvers"))
            on_timeout = step.get("on_timeout", "fail")
            if on_timeout not in {"fail", "approve", "reject"}:
                issues.append(_issue("step.on_timeout_invalid", f"Step '{step_id}' on_timeout must be fail, approve or reject", step_id, "on_timeout"))

        if task_type == "condition" and not step.get("when"):
            issues.append(_issue("step.condition_required", f"Condition step '{step_id}' needs a when expression", step_id, "when"))

        if step.get("type") == "workflow.run":
            workflow_ref = (payload or {}).get("workflow") if isinstance(payload, dict) else None
            if not isinstance(workflow_ref, str) or not workflow_ref.strip():
                issues.append(
                    _issue("step.subworkflow_invalid", f"Step '{step_id}' workflow.run input.workflow must name a published workflow", step_id, "input")
                )

        compensate = step.get("compensate")
        if compensate is not None:
            if not isinstance(compensate, str) or compensate not in known_ids:
                issues.append(_issue("step.compensate_invalid", f"Step '{step_id}' compensate must name another step in this workflow", step_id, "compensate"))
            elif compensate == step_id:
                issues.append(_issue("step.compensate_self", f"Step '{step_id}' cannot compensate itself", step_id, "compensate"))

        if_true = step.get("if_true")
        if_false = step.get("if_false")
        for field_name, branch in (("if_true", if_true), ("if_false", if_false)):
            if branch is None:
                continue
            if not isinstance(branch, str) or branch not in known_ids:
                issues.append(_issue("step.branch_invalid", f"Step '{step_id}' {field_name} must name another step", step_id, field_name))
            elif step_id not in (steps_by_id.get(branch, {}).get("depends_on") or []):
                issues.append(_issue("step.branch_dependency_required", f"Branch '{branch}' must depend on condition step '{step_id}'", step_id, field_name))
        if (if_true is not None or if_false is not None) and task_type != "condition":
            issues.append(_issue("step.branch_type_invalid", f"Step '{step_id}' may set if_true/if_false only when type is condition", step_id, "if_true"))

        if task_type in SYSTEM_TASK_TYPES and foreach:
            issues.append(_issue("step.foreach_invalid", f"Step '{step_id}' of type '{task_type}' cannot use foreach", step_id, "foreach"))

    if issues:
        # Cycles and depth are only meaningful on a structurally sound graph.
        structural = {issue["code"] for issue in issues}
        if structural & {"step.id_duplicate", "step.depends_on_unknown"}:
            return issues

    cycle = find_cycle(edges)
    if cycle:
        issues.append(_issue("graph.cycle", "Workflow contains a dependency cycle: " + " -> ".join(cycle)))
    else:
        depth = max_depth(edges)
        if depth > MAX_STEP_DEPTH:
            issues.append(_issue("graph.too_deep", f"Dependency chain is {depth} levels deep; the limit is {MAX_STEP_DEPTH}"))

    if len(issues) == 0 and not any(step.get("depends_on") for step in steps if isinstance(step, dict)):
        # Not an error, but worth surfacing: nothing runs in parallel order.
        pass

    return issues


def _matches(value: str, pattern: str) -> bool:
    import re

    return re.match(pattern, value) is not None


def find_cycle(edges: dict[str, list[str]]) -> list[str] | None:
    """Return one cycle as a node list, or ``None`` when the graph is acyclic."""
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        if node in visiting:
            return stack[stack.index(node):] + [node]
        if node in visited:
            return None
        visiting.add(node)
        stack.append(node)
        for parent in edges.get(node, []):
            found = visit(parent)
            if found:
                return found
        stack.pop()
        visiting.discard(node)
        visited.add(node)
        return None

    for node in list(edges):
        found = visit(node)
        if found:
            return found
    return None


def max_depth(edges: dict[str, list[str]]) -> int:
    """Longest dependency chain length (1 for a single step)."""
    memo: dict[str, int] = {}

    def depth(node: str) -> int:
        if node in memo:
            return memo[node]
        parents = edges.get(node, [])
        memo[node] = 1 if not parents else 1 + max(depth(parent) for parent in parents)
        return memo[node]

    return max((depth(node) for node in edges), default=0)


def topological_order(edges: dict[str, list[str]]) -> list[str]:
    """Kahn's algorithm over dependency edges. Raises on cycles."""
    indegree = {node: len(set(parents)) for node, parents in edges.items()}
    children: dict[str, list[str]] = defaultdict(list)
    for node, parents in edges.items():
        for parent in set(parents):
            children[parent].append(node)
    queue = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for child in sorted(children.get(node, [])):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(order) != len(edges):
        raise ValueError("Workflow dependencies contain a cycle")
    return order


def levels(edges: dict[str, list[str]]) -> dict[str, int]:
    """Assign each step to a parallel execution level (0 = no dependencies)."""
    result: dict[str, int] = {}
    for node in topological_order(edges):
        parents = edges.get(node, [])
        result[node] = 0 if not parents else 1 + max(result[parent] for parent in parents)
    return result


def downstream_of(edges: dict[str, list[str]], roots: Iterable[str]) -> set[str]:
    """Every step transitively depending on any of ``roots`` (excluding roots)."""
    children: dict[str, list[str]] = defaultdict(list)
    for node, parents in edges.items():
        for parent in parents:
            children[parent].append(node)
    seen: set[str] = set()
    queue = deque(roots)
    while queue:
        node = queue.popleft()
        for child in children.get(node, []):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return seen - set(roots)


def definition_edges(definition: dict[str, Any]) -> dict[str, list[str]]:
    return {step["id"]: list(step.get("depends_on", [])) for step in definition.get("steps", []) if "id" in step}


def summarize_definition(definition: dict[str, Any]) -> dict[str, Any]:
    """Small derived summary used by the API and the UI.

    A *draft* may legitimately be mid-edit and contain a cycle, so this never
    raises: ordering and depth are omitted when the graph is not acyclic.
    """
    edges = definition_edges(definition)
    try:
        order = topological_order(edges) if edges else []
        cyclic = False
    except ValueError:
        order = []
        cyclic = True
    try:
        depth = max_depth(edges) if edges and not cyclic else 0
    except RecursionError:  # pragma: no cover - pathological graph
        depth = 0
    return {
        "step_count": len(edges),
        "edge_count": sum(len(set(parents)) for parents in edges.values()),
        "max_depth": depth,
        "order": order,
        "has_cycle": cyclic,
        "task_types": sorted({step.get("type", "") for step in definition.get("steps", []) if step.get("type")}),
    }


__all__ = [
    "ALLOWED_INPUT_KEYS",
    "MAX_RETRIES",
    "MAX_TIMEOUT_SECONDS",
    "definition_edges",
    "downstream_of",
    "find_cycle",
    "levels",
    "max_depth",
    "summarize_definition",
    "topological_order",
    "validate_definition",
]
