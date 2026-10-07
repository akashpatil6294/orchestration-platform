"""Password hashing, token minting and secret encryption.

Nothing in this module logs its inputs. Worker and API tokens are stored as
keyed hashes (HMAC-SHA256 with a server-side pepper) so a database dump alone is
not enough to impersonate a worker, and secret values are encrypted with
AES-CTR + HMAC (an authenticated, dependency-free construction built on
``hashlib``/``hmac`` so no extra crypto package is required).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.config import settings

ALGORITHM = "HS256"
TOKEN_PREFIX_LENGTH = 8

# Keys whose values must never be persisted in logs, events or outputs.
SENSITIVE_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "credential",
    "private_key",
    "access_key",
    "session",
    "cookie",
    "bearer",
    "signature",
)

REDACTED = "***redacted***"


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Opaque tokens (API + worker)
# --------------------------------------------------------------------------- #
def generate_token(prefix: str = "orch") -> str:
    """Return a fresh, high-entropy bearer token."""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    """Keyed hash of a bearer token; deterministic so it can be looked up."""
    pepper = settings.worker_secret_pepper.encode("utf-8")
    return hmac.new(pepper, token.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_token(token: str, stored_hash: str) -> bool:
    if not token or not stored_hash:
        return False
    return hmac.compare_digest(hash_token(token), stored_hash)


def token_prefix(token: str) -> str:
    """A short, non-secret display fragment (``orch_ab12cd``)."""
    head, _, tail = token.partition("_")
    return f"{head}_{tail[:6]}"


# --------------------------------------------------------------------------- #
# Session tokens (JWT)
# --------------------------------------------------------------------------- #
def create_access_token(user_id: str, *, email: str = "", is_admin: bool = False, ttl_minutes: int | None = None) -> tuple[str, datetime]:
    ttl = ttl_minutes or settings.access_token_ttl_minutes
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl)
    payload: dict[str, Any] = {
        "sub": user_id,
        "email": email,
        "adm": bool(is_admin),
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int(expires_at.timestamp()),
        "typ": "user",
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM), expires_at


def decode_access_token(token: str) -> dict[str, Any] | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != "user":
        return None
    return payload


# --------------------------------------------------------------------------- #
# Secret value encryption (AES-CTR + HMAC-SHA256, encrypt-then-MAC)
# --------------------------------------------------------------------------- #
def _encryption_material() -> bytes:
    material = settings.secrets_encryption_key or settings.secret_key
    return hashlib.sha256(material.encode("utf-8")).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    blocks = bytearray()
    counter = 0
    while len(blocks) < length:
        blocks.extend(hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(blocks[:length])


def encrypt_secret(plaintext: str) -> str:
    key = _encryption_material()
    nonce = os.urandom(12)
    data = plaintext.encode("utf-8")
    stream = _keystream(key, nonce, len(data))
    ciphertext = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii")


def decrypt_secret(token: str) -> str:
    key = _encryption_material()
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise ValueError("Stored secret is not valid base64") from exc
    if len(raw) < 44:
        raise ValueError("Stored secret is truncated")
    nonce, tag, ciphertext = raw[:12], raw[12:44], raw[44:]
    expected = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("Stored secret failed integrity verification")
    stream = _keystream(key, nonce, len(ciphertext))
    return bytes(a ^ b for a, b in zip(ciphertext, stream)).decode("utf-8")


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
def redact(value: Any, *, depth: int = 0, truncate_strings: bool = True) -> Any:
    """Recursively replace sensitive values with a marker.

    Applied to every log line, run event, step output and API error payload.
    """
    if depth > 12:
        return "[depth-limit]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if any(marker in lowered for marker in SENSITIVE_KEY_MARKERS):
                result[key_text] = REDACTED
            else:
                result[key_text] = redact(item, depth=depth + 1, truncate_strings=truncate_strings)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item, depth=depth + 1, truncate_strings=truncate_strings) for item in value]
    if truncate_strings and isinstance(value, str) and len(value) > 4096:
        return value[:4096] + f"...[truncated {len(value) - 4096} chars]"
    return value


def secret_placeholder(name: str) -> str:
    return f"$secret:{name}"


__all__ = [
    "ALGORITHM",
    "REDACTED",
    "create_access_token",
    "decode_access_token",
    "decrypt_secret",
    "encrypt_secret",
    "generate_token",
    "hash_password",
    "hash_token",
    "redact",
    "secret_placeholder",
    "token_prefix",
    "verify_password",
    "verify_token",
]
