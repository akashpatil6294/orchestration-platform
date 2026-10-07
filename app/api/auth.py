"""Authentication routes.

Users: register, sign in, read their session, and mint long-lived API tokens.
Operators: mint worker tokens from the UI so a worker can be started without
shell access to the database.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.auth.dependencies import SupabaseIdentityDep
from app.config import settings
from app.core.errors import Conflict, NotFound, RateLimited, Unauthorized
from app.core.rate_limit import auth_limiter
from app.core.security import (
    create_access_token,
    generate_token,
    hash_password,
    hash_token,
    token_prefix,
    verify_password,
)
from app.models.user import User
from app.models.worker import Worker, WorkerToken
from app.schemas.auth import (
    ApiTokenCreate,
    ApiTokenCreated,
    ApiTokenView,
    LoginRequest,
    RegisterRequest,
    SessionInfo,
    TokenResponse,
    UserView,
    WorkerTokenCreated,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _user_view(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "created_at": user.created_at,
        "initials": user.initials,
        "avatar_url": user.avatar_url,
        "auth_provider": user.auth_provider,
        "has_password": user.has_password,
    }


def _check_auth_rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    allowed, retry_after = auth_limiter().allow(f"auth:{key}")
    if not allowed:
        raise RateLimited(
            "Too many authentication attempts, please try again later",
            code="auth_rate_limited",
            details={"retry_after_seconds": retry_after},
        )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, request: Request, db: DbSession) -> dict:
    _check_auth_rate_limit(request)
    existing = db.scalar(select(User).where(func.lower(User.email) == payload.email.lower()))
    if existing is not None:
        raise Conflict("An account with that email already exists", code="email_taken")
    is_first = int(db.scalar(select(func.count()).select_from(User)) or 0) == 0
    user = User(
        email=payload.email.lower(),
        display_name=payload.display_name or payload.email.split("@")[0],
        password_hash=hash_password(payload.password),
        # The very first account bootstraps the instance and becomes admin.
        is_admin=is_first,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token, expires_at = create_access_token(user.id, email=user.email, is_admin=user.is_admin)
    return {"access_token": token, "token_type": "bearer", "expires_at": expires_at, "user": _user_view(user)}


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: DbSession) -> dict:
    _check_auth_rate_limit(request)
    user = db.scalar(select(User).where(func.lower(User.email) == payload.email.lower()))
    # ``not user.password_hash`` covers accounts created through Google, which
    # have no local password and therefore cannot sign in this way.
    if user is None or not user.password_hash or not verify_password(payload.password, user.password_hash):
        # Same message either way: do not reveal which accounts exist.
        raise Unauthorized("Email or password is incorrect", code="invalid_credentials")
    if not user.is_active:
        raise Unauthorized("This account has been deactivated", code="account_inactive")
    user.last_login_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    db.refresh(user)
    token, expires_at = create_access_token(user.id, email=user.email, is_admin=user.is_admin)
    return {"access_token": token, "token_type": "bearer", "expires_at": expires_at, "user": _user_view(user)}


@router.get("/quota")
def quota(user: CurrentUser, db: DbSession) -> dict:
    """Current quota usage for the signed-in user (drives the UI meters)."""
    from app.services import quota_service

    return quota_service.quota_status(db, triggered_by=user.email)


@router.get("/session", response_model=SessionInfo)
def session(user: CurrentUser, identity: SupabaseIdentityDep) -> dict:
    return {
        "user": _user_view(user),
        "permissions": ["workflows:read", "workflows:write", "runs:read", "runs:write", "schedules:write"] + (["admin"] if user.is_admin else []),
        "environment": settings.environment,
        "features": {
            "scheduler": settings.scheduler_enabled,
            "realtime_transport": "polling",
            "dispatch_backend": "redis-outbox" if settings.redis_url else "database-polling",
            "secrets": True,
        },
        # Complements ``user.auth_provider``: reports which credential this
        # request actually presented.
        "sign_in_method": "google" if identity is not None else "password",
    }


@router.post("/tokens", response_model=ApiTokenCreated, status_code=status.HTTP_201_CREATED)
def create_api_token(payload: ApiTokenCreate, user: CurrentUser, db: DbSession) -> dict:
    token = generate_token("orch")
    record = WorkerToken(
        user_id=user.id,
        name=payload.name,
        token_hash=hash_token(token),
        token_prefix=token_prefix(token),
        scopes=payload.scopes,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return {
        "id": record.id,
        "name": record.name,
        "token_prefix": record.token_prefix,
        "scopes": list(record.scopes or []),
        "created_at": record.created_at,
        "last_used_at": record.last_used_at,
        "revoked_at": record.revoked_at,
        "token": token,
    }


@router.get("/tokens", response_model=list[ApiTokenView])
def list_api_tokens(user: CurrentUser, db: DbSession) -> list[dict]:
    rows = db.scalars(select(WorkerToken).where(WorkerToken.user_id == user.id).order_by(WorkerToken.created_at.desc())).all()
    return [
        {
            "id": row.id,
            "name": row.name,
            "token_prefix": row.token_prefix,
            "scopes": list(row.scopes or []),
            "created_at": row.created_at,
            "last_used_at": row.last_used_at,
            "revoked_at": row.revoked_at,
        }
        for row in rows
    ]


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_api_token(token_id: str, user: CurrentUser, db: DbSession) -> None:
    record = db.get(WorkerToken, token_id)
    if record is None or record.user_id != user.id:
        raise NotFound("Token not found", code="token_not_found")
    record.revoked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()


@router.post("/worker-tokens", response_model=WorkerTokenCreated, status_code=status.HTTP_201_CREATED)
def create_worker_token(
    user: CurrentUser,
    db: DbSession,
    worker_id: str = Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"),
    task_types: str = Query(default="", max_length=2000, description="Comma-separated task type allow-list"),
) -> dict:
    """Mint a worker credential. The plaintext token is shown exactly once."""
    types = [item.strip() for item in task_types.split(",") if item.strip()]
    worker = db.get(Worker, worker_id)
    if worker is not None and worker.token_hash and worker.owner_id not in (None, user.id):
        raise Conflict("That worker id belongs to another account", code="worker_id_taken")
    token = generate_token("wrk")
    if worker is None:
        worker = Worker(id=worker_id, owner_id=user.id)
        db.add(worker)
    worker.owner_id = user.id
    worker.name = worker.name or worker_id
    worker.token_hash = hash_token(token)
    worker.token_prefix = token_prefix(token)
    worker.task_types = types or list(worker.task_types or [])
    worker.active = True
    db.commit()
    db.refresh(worker)
    return {
        "worker_id": worker.id,
        "token": token,
        "token_prefix": worker.token_prefix,
        "task_types": list(worker.task_types or []),
        "created_at": worker.created_at,
    }


__all__ = ["router"]
