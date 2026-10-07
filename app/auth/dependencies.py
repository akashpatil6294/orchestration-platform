"""FastAPI identity dependencies.

``get_current_user`` is the single entry point every protected route uses — it is
the same callable that ``app.core.auth`` exposes, re-exported here so the auth
package presents one obvious interface. Routers keep using the ``CurrentUser``
alias from ``app.api.deps``; there is exactly one implementation, not two
parallel ones.

A bearer token may be any of the three credential types the platform accepts, and
the verifier decides which it is:

* an application session token (``POST /api/v1/auth/login``),
* a Supabase-issued session token (Google sign-in),
* a long-lived API token (``POST /api/v1/auth/tokens``).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header

from app.auth.supabase import SupabaseIdentity, SupabaseTokenError, verify_supabase_token
from app.core.auth import current_user, optional_user
from app.core.logging import get_logger

logger = get_logger("app.auth.dependencies")

#: The canonical required-identity dependency. Raises ``401`` when missing,
#: malformed, expired or unrecognised.
get_current_user = current_user
#: Same, but returns ``None`` instead of raising. Used by routes that adapt their
#: response for signed-in callers.
get_optional_user = optional_user


def get_supabase_identity(authorization: str | None = Header(default=None)) -> SupabaseIdentity | None:
    """Return the verified Supabase identity for this request, if one was used.

    This reports *how* the caller signed in. It never grants access on its own:
    authorization always flows through :func:`get_current_user`.
    """

    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    try:
        return verify_supabase_token(token.strip())
    except SupabaseTokenError as exc:
        logger.debug("Bearer token is not a Supabase session token", extra={"reason": exc.code})
        return None


SupabaseIdentityDep = Annotated[SupabaseIdentity | None, Depends(get_supabase_identity)]

__all__ = [
    "SupabaseIdentityDep",
    "get_current_user",
    "get_optional_user",
    "get_supabase_identity",
]
