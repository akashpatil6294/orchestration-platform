"""Task handler registry.

Workers execute **only** handlers registered here. A workflow definition names a
task type; it can never supply code. Unknown types are rejected by the worker
(and the API refuses reserved ``orchestrator.*`` prefixes at validation time), so
a malformed or hostile definition cannot cause execution of anything unexpected.

Side-effecting handlers should be idempotent: dispatch is at-least-once, and each
task carries ``idempotency_key`` for exactly that purpose.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class TaskContext(Protocol):
    """What a handler receives besides its input."""

    task: dict[str, Any]
    idempotency_key: str
    attempt: int
    run_id: str
    step_key: str
    deadline_at: Any
    api_client: Any  # worker API client (e.g. to fetch document bytes)

    def log(self, message: str, **fields: Any) -> None: ...
    def cancelled(self) -> bool: ...


@dataclass(slots=True)
class Handler:
    """A registered task handler."""

    task_type: str
    func: Callable[[dict[str, Any], TaskContext], Any]
    description: str = ""
    timeout_seconds: int = 300
    side_effects: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)
    # --- Builder metadata (H1) -------------------------------------------------
    # JSON Schema describing the step input the handler accepts. The visual
    # builder renders its inspector from this; it is documentation, not a
    # runtime gate (handlers keep their own input validation).
    input_schema: dict[str, Any] = field(default_factory=dict)
    # JSON Schema describing the handler's output shape, when known.
    output_schema: dict[str, Any] | None = None
    # Builder palette grouping, e.g. "ai", "document", "connector".
    category: str = ""
    # Icon key for the builder palette; the frontend maps it to a glyph.
    icon_key: str = ""
    # True when re-executing the handler with the same input is safe.
    # The retry policy UI warns when retries are enabled on a task whose
    # handler is not idempotent.
    idempotent: bool = False
    # Recommended execution policy shown in the builder's policy section:
    # {"timeout_seconds": int, "max_retries": int, "backoff": "fixed"|"exponential",
    #  "max_delay_seconds": int, "jitter": bool}.
    default_policy: dict[str, Any] = field(default_factory=dict)


class TaskRegistry:
    """An explicit allow-list of task handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(
        self,
        task_type: str,
        *,
        description: str = "",
        timeout_seconds: int = 300,
        side_effects: bool = False,
        tags: tuple[str, ...] = (),
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        category: str = "",
        icon_key: str = "",
        idempotent: bool = False,
        default_policy: dict[str, Any] | None = None,
    ) -> Callable[[Callable[[dict[str, Any], TaskContext], Any]], Callable[[dict[str, Any], TaskContext], Any]]:
        def decorator(func: Callable[[dict[str, Any], TaskContext], Any]) -> Callable[[dict[str, Any], TaskContext], Any]:
            if task_type in self._handlers:
                raise ValueError(f"Task type '{task_type}' is already registered")
            self._handlers[task_type] = Handler(
                task_type=task_type,
                func=func,
                description=description or (func.__doc__ or "").strip().split("\n")[0],
                timeout_seconds=timeout_seconds,
                side_effects=side_effects,
                tags=tags,
                input_schema=dict(input_schema or {}),
                output_schema=dict(output_schema) if output_schema else None,
                category=category,
                icon_key=icon_key,
                idempotent=idempotent,
                default_policy=dict(default_policy or {}),
            )
            return func

        return decorator

    def add(self, handler: Handler) -> None:
        if handler.task_type in self._handlers:
            raise ValueError(f"Task type '{handler.task_type}' is already registered")
        self._handlers[handler.task_type] = handler

    def get(self, task_type: str) -> Handler | None:
        return self._handlers.get(task_type)

    def resolve(self, task_type: str) -> Handler:
        handler = self._handlers.get(task_type)
        if handler is None:
            raise UnsupportedTaskType(
                f"This worker cannot execute '{task_type}'. Supported types: {', '.join(self.types()) or 'none'}"
            )
        return handler

    def types(self) -> list[str]:        return sorted(self._handlers)

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "task_type": handler.task_type,
                "description": handler.description,
                "timeout_seconds": handler.timeout_seconds,
                "side_effects": handler.side_effects,
                "tags": list(handler.tags),
                "input_schema": handler.input_schema,
                "output_schema": handler.output_schema,
                "category": handler.category,
                "icon_key": handler.icon_key,
                "idempotent": handler.idempotent,
                "default_policy": handler.default_policy,
            }
            for handler in sorted(self._handlers.values(), key=lambda item: item.task_type)
        ]

    def __contains__(self, task_type: object) -> bool:
        return isinstance(task_type, str) and task_type in self._handlers

    def __len__(self) -> int:
        return len(self._handlers)


class UnsupportedTaskType(Exception):
    """Raised when a claimed task names a type this worker does not implement."""

    retryable = False


registry = TaskRegistry()

__all__ = ["Handler", "TaskContext", "TaskRegistry", "UnsupportedTaskType", "registry"]
