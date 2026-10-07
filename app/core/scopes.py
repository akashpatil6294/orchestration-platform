"""API-token scope enforcement.

Scopes form a hierarchy: ``manage`` > ``run`` > ``read``. A token granted
``manage`` may do everything; ``run`` may read and trigger actions; ``read``
may only read. Session JWTs (interactive sign-in) always carry full access —
scopes only constrain long-lived API tokens.

Routes declare their required scope explicitly via the ``requires_scope``
decorator; anything without an explicit declaration falls back to a
method-based default (GET -> read, DELETE -> manage, everything else -> run).
"""
from __future__ import annotations

READ = "read"
RUN = "run"
MANAGE = "manage"

SCOPES = (READ, RUN, MANAGE)
_LEVELS = {READ: 1, RUN: 2, MANAGE: 3}


def scope_covers(granted: list[str], required: str) -> bool:
    """True when any granted scope meets or exceeds the required level."""
    required_level = _LEVELS.get(required, 99)
    return any(_LEVELS.get(scope, 0) >= required_level for scope in granted)


def default_scope_for(method: str) -> str:
    """Method-based fallback when a route declares no explicit scope."""
    if method == "GET":
        return READ
    if method == "DELETE":
        return MANAGE
    return RUN


def requires_scope(scope: str):
    """Decorator marking a route's required API-token scope."""
    if scope not in SCOPES:
        raise ValueError(f"Unknown scope {scope!r}; expected one of {SCOPES}")

    def decorator(func):
        func.__required_scope__ = scope
        return func

    return decorator


def required_scope_for(route) -> str:
    """Resolve the effective required scope for a Starlette route."""
    explicit = getattr(route, "__required_scope__", None)
    if explicit is None and hasattr(route, "endpoint"):
        explicit = getattr(route.endpoint, "__required_scope__", None)
    if explicit:
        return explicit
    methods = getattr(route, "methods", None) or {"GET"}
    return default_scope_for(sorted(methods)[0])


__all__ = [
    "MANAGE",
    "READ",
    "RUN",
    "SCOPES",
    "default_scope_for",
    "required_scope_for",
    "requires_scope",
    "scope_covers",
]
