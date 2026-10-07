"""Verification of Supabase-issued access tokens.

The browser receives a signed access token from Supabase after Google sign-in and
sends it to the API as ``Authorization: Bearer <token>``. This module decides
whether that token can be trusted. It is deliberately independent from
``app.core.security``, which mints and verifies *application* credentials: mixing
the two would make a session token indistinguishable from an API token.

Two signing schemes are supported, chosen by the token's own header:

* **Asymmetric (default for new Supabase projects).** Tokens are ``ES256`` or
  ``RS256`` and carry a ``kid``. Only the public key set is downloaded from the
  project's JWKS endpoint and it is cached in memory.
* **Legacy shared secret.** ``SUPABASE_JWT_SECRET`` with ``HS256``.

The shared secret is *never* considered for a token that advertises another
algorithm, so an attacker cannot downgrade an asymmetric token to a symmetric
check. ``alg: none`` is rejected outright.

Nothing here logs, returns or stores the token itself; failures raise a
:class:`SupabaseTokenError` whose ``code`` and message are safe to log.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import jwt

from app.config import settings
from app.core.logging import get_logger

logger = get_logger("app.auth.supabase")

SYMMETRIC_ALGORITHM = "HS256"
ASYMMETRIC_ALGORITHMS = ("ES256", "RS256")
ALLOWED_ALGORITHMS = (SYMMETRIC_ALGORITHM, *ASYMMETRIC_ALGORITHMS)
# Tolerated clock skew between this server and Supabase when checking ``exp``.
LEEWAY_SECONDS = 30.0
# Key types PyJWT can load from the JWKS document.
SUPPORTED_KEY_TYPES = ("EC", "RSA")
# A public key set is a few kilobytes; refuse anything implausibly large.
MAX_JWKS_BYTES = 262_144


class SupabaseTokenError(Exception):
    """A Supabase token could not be trusted.

    ``code`` is a stable machine-readable reason; ``message`` is written for
    operators and never contains token material.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class SupabaseIdentity:
    """The subset of a verified Supabase token the platform relies on."""

    subject: str
    email: str = ""
    display_name: str = ""
    avatar_url: str = ""
    provider: str = "google"
    role: str = ""
    expires_at: int | None = None
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def is_anonymous(self) -> bool:
        """Supabase issues anonymous sessions whose subject is ``anon``."""

        return self.subject in {"", "anon"} or self.role == "anon"


# --------------------------------------------------------------------------- #
# JWKS cache
# --------------------------------------------------------------------------- #
class _JwksCache:
    """In-memory cache of the project's public keys, keyed by URL."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, url: str, *, max_age: int) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            cached = self._entries.get(url)
            if cached is not None and now - cached[0] < max_age:
                return cached[1]
        document = _download_jwks(url)
        with self._lock:
            self._entries[url] = (now, document)
        return document

    def clear(self, url: str | None = None) -> None:
        with self._lock:
            if url is None:
                self._entries.clear()
            else:
                self._entries.pop(url, None)


_jwks_cache = _JwksCache()


def clear_jwks_cache() -> None:
    """Test helper: drop cached public keys."""

    _jwks_cache.clear()


def _download_jwks(url: str) -> dict[str, Any]:
    """Fetch the project's public keys.

    Uses the standard library on purpose: this is the only outbound request the
    API ever makes, it retrieves public key material only, and keeping it
    dependency-free avoids coupling authentication to an HTTP client's release
    cadence. The URL comes from configuration (``SUPABASE_URL``), never from a
    request.
    """

    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "orchestration-platform/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.supabase_jwks_timeout_seconds) as response:  # noqa: S310
            raw = response.read(MAX_JWKS_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # The URL is configuration, not a credential, so it is safe to log.
        logger.warning("Supabase public key set returned an error", extra={"url": url, "status": exc.code})
        raise SupabaseTokenError("jwks_unavailable", "Supabase public keys could not be fetched") from exc
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("Could not reach the Supabase public key set", extra={"url": url, "error": type(exc).__name__})
        raise SupabaseTokenError("jwks_unavailable", "Supabase public keys could not be fetched") from exc
    except Exception as exc:  # pragma: no cover - defensive
        # Verification must fail closed. An unexpected transport error has to
        # become a rejected credential, never a 500 or a pass.
        logger.warning("Unexpected error fetching the Supabase key set", extra={"url": url, "error": type(exc).__name__})
        raise SupabaseTokenError("jwks_unavailable", "Supabase public keys could not be fetched") from exc

    if len(raw) > MAX_JWKS_BYTES:
        raise SupabaseTokenError("jwks_malformed", "Supabase public key set is unexpectedly large")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SupabaseTokenError("jwks_malformed", "Supabase public key set is not valid JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise SupabaseTokenError("jwks_malformed", "Supabase public key set is malformed")
    return document


def _parse_jwks(document: dict[str, Any]) -> jwt.PyJWKSet:
    """Load the usable keys, ignoring any key type PyJWT cannot verify with."""

    usable = [key for key in document.get("keys", []) if isinstance(key, dict) and key.get("kty") in SUPPORTED_KEY_TYPES]
    if not usable:
        raise SupabaseTokenError("jwks_empty", "Supabase published no usable signing keys")
    try:
        return jwt.PyJWKSet.from_dict({"keys": usable})
    except Exception as exc:  # pragma: no cover - depends on remote key material
        raise SupabaseTokenError("jwks_malformed", "Supabase public key set could not be parsed") from exc


def _asymmetric_signing_key(kid: str | None) -> Any:
    url = settings.supabase_jwks_url_resolved
    key_set = _parse_jwks(_jwks_cache.get(url, max_age=settings.supabase_jwks_cache_seconds))
    if kid:
        try:
            return key_set[str(kid)].key
        except KeyError:
            # Supabase rotates signing keys. Refresh once before giving up so a
            # rotation is picked up without restarting the API.
            _jwks_cache.clear(url)
            key_set = _parse_jwks(_jwks_cache.get(url, max_age=settings.supabase_jwks_cache_seconds))
            try:
                return key_set[str(kid)].key
            except KeyError as exc:
                logger.warning("Access token was signed with an unknown key", extra={"kid": str(kid)})
                raise SupabaseTokenError("unknown_key", "Access token was signed with an unknown key") from exc
    available = [key for key in key_set.keys]
    if len(available) == 1:
        return available[0].key
    raise SupabaseTokenError("unknown_key", "Access token does not identify which signing key was used")


def _signing_key(algorithm: str, header: dict[str, Any]) -> Any:
    if algorithm == SYMMETRIC_ALGORITHM:
        secret = (settings.supabase_jwt_secret or "").strip()
        if not secret:
            raise SupabaseTokenError(
                "shared_secret_missing",
                "This project signs tokens with a shared secret but SUPABASE_JWT_SECRET is not set",
            )
        return secret
    return _asymmetric_signing_key(header.get("kid"))


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _identity_from_claims(claims: dict[str, Any]) -> SupabaseIdentity:
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise SupabaseTokenError("missing_subject", "Access token does not identify a user")

    metadata = claims.get("user_metadata")
    app_metadata = claims.get("app_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    app_metadata = app_metadata if isinstance(app_metadata, dict) else {}

    expires_at = claims.get("exp")
    return SupabaseIdentity(
        subject=subject,
        email=_first_text(claims.get("email")),
        display_name=_first_text(
            metadata.get("full_name"),
            metadata.get("name"),
            metadata.get("display_name"),
            metadata.get("preferred_username"),
        ),
        avatar_url=_first_text(metadata.get("avatar_url"), metadata.get("picture")),
        provider=_first_text(app_metadata.get("provider"), "google"),
        role=_first_text(claims.get("role")),
        expires_at=int(expires_at) if isinstance(expires_at, (int, float)) else None,
        claims=claims,
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def verify_supabase_token(token: str) -> SupabaseIdentity:
    """Verify a Supabase access token and return the identity it asserts.

    Raises :class:`SupabaseTokenError` when the token is missing, malformed,
    expired, issued by another project, or not signed by this project's keys.
    """

    candidate = (token or "").strip()
    if not candidate:
        raise SupabaseTokenError("missing_token", "No access token was supplied")
    if not settings.supabase_auth_enabled:
        raise SupabaseTokenError("supabase_disabled", "Supabase sign-in is not configured on this server")

    try:
        header = jwt.get_unverified_header(candidate)
    except jwt.PyJWTError as exc:
        raise SupabaseTokenError("malformed_token", "Access token is malformed") from exc

    algorithm = str(header.get("alg") or "")
    if algorithm not in ALLOWED_ALGORITHMS:
        raise SupabaseTokenError("unsupported_algorithm", f"Access token algorithm is not accepted: {algorithm or 'none'}")

    key = _signing_key(algorithm, header)
    try:
        claims = jwt.decode(
            candidate,
            key,
            algorithms=[algorithm],
            audience=settings.supabase_jwt_audience,
            issuer=settings.supabase_issuer,
            leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise SupabaseTokenError("token_expired", "Access token has expired") from exc
    except jwt.ImmatureSignatureError as exc:
        raise SupabaseTokenError("token_not_yet_valid", "Access token is not valid yet") from exc
    except jwt.InvalidAudienceError as exc:
        raise SupabaseTokenError("invalid_audience", "Access token was issued for a different audience") from exc
    except jwt.InvalidIssuerError as exc:
        raise SupabaseTokenError("invalid_issuer", "Access token was issued by a different project") from exc
    except jwt.InvalidSignatureError as exc:
        raise SupabaseTokenError("invalid_signature", "Access token signature could not be verified") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise SupabaseTokenError("missing_claim", "Access token is missing a required claim") from exc
    except jwt.PyJWTError as exc:
        raise SupabaseTokenError("invalid_token", "Access token could not be verified") from exc

    identity = _identity_from_claims(claims)
    if identity.is_anonymous:
        raise SupabaseTokenError("anonymous_session", "Anonymous Supabase sessions cannot access this API")
    return identity


def _jwt_role(token: str) -> str:
    """Read the ``role`` claim without verifying. For configuration checks only."""

    candidate = (token or "").strip()
    if not candidate:
        return ""
    try:
        claims = jwt.decode(candidate, options={"verify_signature": False, "verify_exp": False})
    except jwt.PyJWTError:
        return ""
    role = claims.get("role") if isinstance(claims, dict) else None
    return role if isinstance(role, str) else ""


def describe_configuration() -> dict[str, Any]:
    """Non-secret summary of the Supabase configuration, for ``doctor`` and ops.

    Reports *whether* a value is set and never the value itself.
    """

    origin = _jwt_role(settings.supabase_anon_key)
    return {
        "enabled": settings.supabase_auth_enabled,
        "project_url": settings.supabase_base_url,
        "issuer": settings.supabase_issuer if settings.supabase_auth_enabled else "",
        "verification": settings.supabase_verification_mode,
        "jwks_url": settings.supabase_jwks_url_resolved if settings.supabase_auth_enabled else "",
        "anon_key_configured": bool(settings.supabase_anon_key),
        # A service-role key must never be reachable from a browser, and the
        # backend does not accept one either: tokens signed for it are refused
        # because ``aud`` is ``authenticated``.
        "privileged_key_present": origin == "service_role",
        "password_auth_enabled": settings.password_auth_enabled,
    }


__all__ = [
    "ALLOWED_ALGORITHMS",
    "LEEWAY_SECONDS",
    "SupabaseIdentity",
    "SupabaseTokenError",
    "clear_jwks_cache",
    "describe_configuration",
    "verify_supabase_token",
]
