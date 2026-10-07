"""Supabase-backed authentication.

Supabase Auth is used **only** to establish who the caller is (Google sign-in and
email/password inside Supabase). Everything the platform stores — users,
workflows, versions, runs, steps, attempts, events, schedules, workers and leases
— lives in the application database and is reached through the existing
SQLAlchemy engine. No Supabase database API and no service-role credential is
used anywhere in this package.

Layering:

``supabase``
    Verifies a Supabase-issued access token and returns a small, typed identity.
``service``
    Maps that identity onto an application :class:`~app.models.user.User`,
    creating or linking the row exactly once.
``dependencies``
    FastAPI dependencies. ``get_current_user`` is the single entry point every
    protected route uses.
"""
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.auth.dependencies import SupabaseIdentityDep, get_current_user, get_optional_user
    from app.auth.service import resolve_supabase_user
    from app.auth.supabase import SupabaseIdentity, SupabaseTokenError, verify_supabase_token

__all__ = [
    "SupabaseIdentity",
    "SupabaseIdentityDep",
    "SupabaseTokenError",
    "get_current_user",
    "get_optional_user",
    "resolve_supabase_user",
    "verify_supabase_token",
]

_LAZY: dict[str, str] = {
    "get_current_user": "app.auth.dependencies",
    "get_optional_user": "app.auth.dependencies",
    "get_supabase_identity": "app.auth.dependencies",
    "SupabaseIdentityDep": "app.auth.dependencies",
    "resolve_supabase_user": "app.auth.service",
    "SupabaseIdentity": "app.auth.supabase",
    "SupabaseTokenError": "app.auth.supabase",
    "verify_supabase_token": "app.auth.supabase",
}


def __getattr__(name: str) -> Any:
    """Resolve the public names lazily.

    ``app.core.auth`` imports this package, and ``app.auth.dependencies`` imports
    back from ``app.core.auth``. Importing the submodules eagerly here would close
    that loop while ``app.core.auth`` is still executing, so they are loaded on
    first access instead.
    """

    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_name), name)
