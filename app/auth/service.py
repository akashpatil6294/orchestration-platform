"""Map a verified Supabase identity onto an application account.

The application database, not Supabase, stays the source of truth for users.
Supabase only answers "who is this?", and this module translates that answer into
a row in ``users`` exactly once per person.

Resolution order:

1. **By ``supabase_user_id``.** The normal path after the first sign-in. The
   subject id is the durable identity key, so a changed Google address still
   resolves to the same account.
2. **By email, for provider-only accounts.** If an account exists with the same
   address that has *no* password and *no* linked identity, it is adopted. This
   keeps older provider accounts from being duplicated.
3. **Create.** A brand-new account, with no password.

An account protected by a password is never adopted automatically. Its email was
never verified by this platform, so linking on an address match alone would let
whoever registered the address first inherit someone else's Google identity.
Those sign-ins are refused with a clear, actionable error instead.

Authorization always reads the application row (``users.id``); nothing supplied
by the browser is trusted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.supabase import SupabaseIdentity
from app.core.errors import Conflict, Unauthorized
from app.core.logging import get_logger
from app.models.base import utc, utcnow
from app.models.user import User

logger = get_logger("app.auth.service")

# ``last_login_at`` is refreshed at most this often so an authenticated read does
# not turn into a database write on every request.
LAST_SEEN_REFRESH = timedelta(minutes=5)
# RFC 2606 reserves ``.invalid``; used only when a provider reports no address.
PLACEHOLDER_EMAIL_DOMAIN = "users.invalid"


def _normalised_email(email: str) -> str:
    return (email or "").strip().lower()


def _display_fallback(email: str) -> str:
    local = email.split("@")[0].strip()
    return local or "User"


def _is_first_account(db: Session) -> bool:
    """Mirror ``POST /auth/register``: the first account bootstraps admin."""

    return int(db.scalar(select(func.count()).select_from(User)) or 0) == 0


def _by_subject(db: Session, subject: str) -> User | None:
    return db.scalar(select(User).where(User.supabase_user_id == subject))


def _apply_profile(user: User, identity: SupabaseIdentity) -> bool:
    """Copy display fields from the identity. Returns True when something changed."""

    changed = False
    if identity.display_name and user.display_name != identity.display_name:
        user.display_name = identity.display_name
        changed = True
    if identity.avatar_url and user.avatar_url != identity.avatar_url:
        user.avatar_url = identity.avatar_url
        changed = True
    if identity.provider and user.auth_provider != identity.provider:
        user.auth_provider = identity.provider
        changed = True
    return changed


def _link_by_email(db: Session, identity: SupabaseIdentity) -> User | None:
    """Adopt a password-less account that already uses this address."""

    email = _normalised_email(identity.email)
    if not email:
        return None
    candidate = db.scalar(select(User).where(func.lower(User.email) == email))
    if candidate is None:
        return None
    if candidate.supabase_user_id == identity.subject:
        return candidate
    if candidate.supabase_user_id:
        raise Conflict(
            "This email is already connected to a different sign-in method. Use that account instead.",
            code="email_in_use",
        )
    if candidate.password_hash:
        # Deliberately not linked: see the module docstring.
        raise Conflict(
            "An account with this email already exists. Sign in with your email and password, "
            "or use a different Google account.",
            code="email_in_use",
        )
    candidate.supabase_user_id = identity.subject
    _apply_profile(candidate, identity)
    db.commit()
    db.refresh(candidate)
    logger.info("Linked a federated identity to an existing account", extra={"user_id": candidate.id})
    return candidate


def _create_user(db: Session, identity: SupabaseIdentity) -> User:
    email = _normalised_email(identity.email) or f"supabase-{identity.subject}@{PLACEHOLDER_EMAIL_DOMAIN}"
    user = User(
        email=email,
        display_name=identity.display_name or _display_fallback(email),
        # Federated accounts have no local password; NULL marks that clearly.
        password_hash=None,
        supabase_user_id=identity.subject,
        auth_provider=identity.provider or "google",
        avatar_url=identity.avatar_url or None,
        is_admin=_is_first_account(db),
        last_login_at=utcnow(),
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        # A concurrent request created the same account first; reuse it.
        db.rollback()
        existing = _by_subject(db, identity.subject)
        if existing is not None:
            return existing
        existing = db.scalar(select(User).where(func.lower(User.email) == email))
        if existing is not None and existing.supabase_user_id == identity.subject:
            return existing
        raise Conflict("Could not create your account; please sign in again", code="account_conflict") from None
    db.commit()
    db.refresh(user)
    logger.info(
        "Created an application account for a federated sign-in",
        extra={"user_id": user.id, "provider": user.auth_provider, "admin": user.is_admin},
    )
    return user


def resolve_supabase_user(db: Session, identity: SupabaseIdentity) -> User:
    """Return the application account for a verified Supabase identity."""

    user = _by_subject(db, identity.subject)
    if user is None:
        user = _link_by_email(db, identity)
    if user is None:
        user = _create_user(db, identity)

    if not user.is_active:
        raise Unauthorized("This account has been deactivated", code="account_inactive")

    changed = _apply_profile(user, identity)
    now = datetime.now(timezone.utc)
    last_login_at = utc(user.last_login_at)
    if last_login_at is None or (now - last_login_at) > LAST_SEEN_REFRESH:
        user.last_login_at = utcnow()
        changed = True
    if changed:
        db.commit()
    return user


__all__ = ["LAST_SEEN_REFRESH", "resolve_supabase_user"]
