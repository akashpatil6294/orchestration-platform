"""Authentication and authorisation dependencies.

Two independent principals exist:

* **Users** present a bearer token. Three credential types are accepted and the
  verifier works out which one it is:

  1. an application session token minted by ``POST /api/v1/auth/login``;
  2. a Supabase session token issued after Google sign-in, mapped onto an
     application account by :mod:`app.auth.service`;
  3. a long-lived API token minted by ``POST /api/v1/auth/tokens``.

  Ownership is enforced by every service query, not by the route layer alone.
* **Workers** authenticate with a bearer token issued by an operator. Worker
  routes never accept user credentials, and user routes never accept worker
  credentials, so a compromised worker cannot read workflow history.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.service import resolve_supabase_user
from app.auth.supabase import SupabaseTokenError, verify_supabase_token
from app.config import settings
from app.core.errors import Forbidden, Unauthorized
from app.core.logging import get_logger, user_id_var
from app.core.security import decode_access_token, hash_token, verify_token
from app.database import get_db
from app.models.user import User
from app.models.worker import Worker, WorkerToken

logger = get_logger("app.core.auth")


@dataclass(slots=True)
class Principal:
    """The authenticated caller."""

    kind: str  # "user" | "worker"
    id: str
    email: str = ""
    display_name: str = ""
    is_admin: bool = False
    worker: Worker | None = None
    scopes: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return self.display_name or self.email or self.id


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _looks_like_jwt(token: str) -> bool:
    """Cheap shape check so opaque API tokens skip JWT parsing.

    Application and API tokens are minted as ``orch_...``/``wrk_...``, so they
    never reach the JWT verifiers at all.
    """

    if token.count(".") != 2 or not token.startswith("ey"):
        return False
    header, _, signature = token.partition(".")
    return bool(header and signature)


def _user_from_supabase_token(db: Session, token: str) -> User | None:
    """Map a Supabase session token onto its application account."""

    if not settings.supabase_auth_enabled:
        return None
    try:
        identity = verify_supabase_token(token)
    except SupabaseTokenError as exc:
        # ``code``/``message`` are safe to log; the token itself never is.
        logger.info("Rejected a Supabase session token", extra={"reason": exc.code})
        return None
    return resolve_supabase_user(db, identity)


def _user_from_token(db: Session, token: str, request: Request | None = None) -> User | None:
    if _looks_like_jwt(token):
        # 1. Application session token (``POST /api/v1/auth/login``).
        payload = decode_access_token(token)
        if payload:
            user = db.get(User, payload.get("sub", ""))
            return user if user and user.is_active else None

        # 2. Supabase session token (Google sign-in). This creates or links the
        #    application account the first time an identity is seen.
        user = _user_from_supabase_token(db, token)
        if user is not None:
            return user if user.is_active else None
        return None

    # 3. Long-lived API token.
    record = db.scalar(select(WorkerToken).where(WorkerToken.token_hash == hash_token(token)))
    if not record or record.revoked_at is not None:
        return None
    if record.expires_at is not None and record.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
        return None
    record.last_used_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.flush()
    if request is not None:
        # Session JWTs carry full access; API tokens are scope-constrained.
        request.state.api_token_scopes = list(record.scopes or [])
        request.state.api_token_id = record.id
    return db.get(User, record.user_id)


def current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    token = _bearer(authorization)
    if not token:
        raise Unauthorized("Sign in to continue", code="missing_credentials")
    user = _user_from_token(db, token, request)
    if not user:
        raise Unauthorized("Your session has expired or is invalid", code="invalid_credentials")
    user_id_var.set(user.id)
    request.state.user_id = user.id
    # API-token scope enforcement: session JWTs carry full access, but
    # long-lived API tokens are constrained to their granted scopes.
    token_scopes = getattr(request.state, "api_token_scopes", None)
    if token_scopes is not None:
        from app.core.scopes import required_scope_for, scope_covers

        route = request.scope.get("route")
        required = required_scope_for(route) if route is not None else "read"
        if not scope_covers(token_scopes, required):
            raise Forbidden(
                f"This API token does not have the '{required}' scope",
                code="insufficient_scope",
            )
    return user


def optional_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User | None:
    token = _bearer(authorization)
    if not token:
        return None
    return _user_from_token(db, token)


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise Forbidden("Administrator access is required", code="admin_required")
    return user


def _authenticate_worker(
    authorization: str | None,
    db: Session,
    *,
    allow_inactive: bool,
) -> Worker:
    """Resolve a worker credential, optionally tolerating an inactive worker."""
    token = _bearer(authorization)
    if not token:
        raise Unauthorized("Worker token is required", code="missing_worker_token")
    worker = db.scalar(select(Worker).where(Worker.token_hash == hash_token(token)))
    if not worker or not verify_token(token, worker.token_hash):
        raise Unauthorized("Worker token is not recognised", code="invalid_worker_token")
    if not worker.active and not allow_inactive:
        raise Forbidden("This worker has been deactivated", code="worker_inactive")
    return worker


def current_worker(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Worker:
    """Authenticate a worker by its bearer token."""
    return _authenticate_worker(authorization, db, allow_inactive=False)


def registering_worker(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Worker:
    """Authenticate a worker that is (re)registering.

    Registration is how a worker comes back online after a graceful stop, which
    deactivates it, so it is the one protocol route an inactive worker may call.
    """
    return _authenticate_worker(authorization, db, allow_inactive=True)


__all__ = [
    "Principal",
    "current_user",
    "current_worker",
    "optional_user",
    "registering_worker",
    "require_admin",
]
