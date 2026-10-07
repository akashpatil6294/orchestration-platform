"""Supabase / Google sign-in authentication tests.

No real Google login happens here. Instead a locally minted, correctly signed
Supabase access token stands in for the one the browser would receive, which lets
every verification branch be exercised deterministically: valid identity, wrong
secret, expired, wrong issuer, wrong audience, ``alg: none``, the asymmetric JWKS
path, and the application-user mapping rules.
"""
from __future__ import annotations

import base64
import json
import time
import uuid

import jwt
import pytest

from app import config as config_module
from app.auth import supabase as supabase_module
from app.core import auth as core_auth

SUPABASE_PROJECT_URL = "https://testproject.supabase.co"
SUPABASE_SHARED_SECRET = "test-supabase-shared-secret-0123456789abcdef"
SUPABASE_ISSUER = f"{SUPABASE_PROJECT_URL}/auth/v1"
DEFAULT_PASSWORD = "testpassword123"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _settings_objects() -> list:
    """Every module-level ``settings`` the auth code might read.

    ``app.core.auth`` and ``app.auth.supabase`` import the settings object by
    reference, so a test patches the object(s) actually in use rather than
    re-creating one.
    """

    seen: set[int] = set()
    objects: list = []
    for module in (config_module, supabase_module, core_auth):
        candidate = getattr(module, "settings", None)
        if candidate is not None and id(candidate) not in seen:
            seen.add(id(candidate))
            objects.append(candidate)
    return objects


@pytest.fixture()
def supabase_auth(monkeypatch):
    """Point the running app at a test Supabase project using HMAC signing."""

    for settings in _settings_objects():
        monkeypatch.setattr(settings, "supabase_url", SUPABASE_PROJECT_URL)
        monkeypatch.setattr(settings, "supabase_jwt_secret", SUPABASE_SHARED_SECRET)
        monkeypatch.setattr(settings, "supabase_jwt_audience", "authenticated")
        monkeypatch.setattr(settings, "supabase_jwks_url", "")
    supabase_module.clear_jwks_cache()
    yield
    supabase_module.clear_jwks_cache()


def supabase_token(
    *,
    subject: str | None = None,
    email: str | None = None,
    secret: str = SUPABASE_SHARED_SECRET,
    audience: str = "authenticated",
    issuer: str = SUPABASE_ISSUER,
    expires_in: int = 3600,
    display_name: str = "Ada Lovelace",
    avatar_url: str = "https://example.test/avatar.png",
    provider: str = "google",
) -> str:
    now = int(time.time())
    payload = {
        "sub": subject or str(uuid.uuid4()),
        "aud": audience,
        "iss": issuer,
        "iat": now,
        "exp": now + expires_in,
        "email": email or f"google-{uuid.uuid4().hex[:10]}@example.com",
        "role": "authenticated",
        "user_metadata": {"full_name": display_name, "avatar_url": avatar_url},
        "app_metadata": {"provider": provider},
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def google_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unsigned_token(**claims) -> str:
    """An ``alg: none`` token: its payload is honest but it carries no signature."""

    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(claims).encode())
    return f"{header}.{payload}."


def _user_rows(email: str) -> list:
    from sqlalchemy import func, select

    from app.database import SessionLocal
    from app.models.user import User

    with SessionLocal() as db:
        return list(db.scalars(select(User).where(func.lower(User.email) == email.lower())).all())


# --------------------------------------------------------------------------- #
# 1-3. Rejection of missing / malformed / expired credentials
# --------------------------------------------------------------------------- #
def test_unauthenticated_request_gets_401(client, supabase_auth):
    response = client.get("/api/v1/workflows")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_credentials"

    no_scheme = client.get("/api/v1/workflows", headers={"Authorization": "token abc"})
    assert no_scheme.status_code == 401


def test_invalid_jwt_is_rejected(client, supabase_auth):
    forged = supabase_token(secret="a-completely-different-shared-secret-value")
    response = client.get("/api/v1/workflows", headers=google_headers(forged))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"

    garbage = client.get("/api/v1/workflows", headers=google_headers("eyJhbGciOiJIUzI1NiJ9.not-a-jwt"))
    assert garbage.status_code == 401

    unsigned = client.get("/api/v1/workflows", headers=google_headers(unsigned_token(sub="x", aud="authenticated")))
    assert unsigned.status_code == 401


def test_expired_token_is_rejected(client, supabase_auth):
    expired = supabase_token(expires_in=-3600)
    response = client.get("/api/v1/workflows", headers=google_headers(expired))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_token_from_another_project_or_audience_is_rejected(client, supabase_auth):
    other_project = supabase_token(issuer="https://someone-else.supabase.co/auth/v1")
    assert client.get("/api/v1/workflows", headers=google_headers(other_project)).status_code == 401

    wrong_audience = supabase_token(audience="service_role")
    assert client.get("/api/v1/workflows", headers=google_headers(wrong_audience)).status_code == 401


def test_supabase_tokens_are_ignored_when_supabase_is_not_configured(client, monkeypatch):
    for settings in _settings_objects():
        monkeypatch.setattr(settings, "supabase_url", "")
    token = supabase_token()
    assert client.get("/api/v1/workflows", headers=google_headers(token)).status_code == 401


def test_anonymous_supabase_sessions_are_rejected(client, supabase_auth):
    response = client.get("/api/v1/workflows", headers=google_headers(supabase_token(subject="anon")))
    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# 4. A valid identity authenticates
# --------------------------------------------------------------------------- #
def test_valid_supabase_identity_is_authenticated(client, supabase_auth):
    email = f"valid-{uuid.uuid4().hex[:8]}@example.com"
    token = supabase_token(email=email, display_name="Grace Hopper")
    session = client.get("/api/v1/auth/session", headers=google_headers(token))
    assert session.status_code == 200, session.text
    body = session.json()
    assert body["user"]["email"] == email
    assert body["user"]["display_name"] == "Grace Hopper"
    assert body["user"]["auth_provider"] == "google"
    assert body["user"]["has_password"] is False
    assert body["sign_in_method"] == "google"
    assert "workflows:read" in body["permissions"]


def test_supabase_identity_can_load_dashboard(client, supabase_auth):
    email = f"dashboard-{uuid.uuid4().hex[:8]}@example.com"
    token = supabase_token(email=email, display_name="Dashboard User")

    response = client.get("/api/v1/runs/dashboard", headers=google_headers(token))

    assert response.status_code == 200, response.text
    assert "stats" in response.json()


def test_existing_supabase_user_accepts_timezone_aware_last_login(db_session, monkeypatch):
    from datetime import datetime, timezone

    from app.auth import service as auth_service
    from app.auth.supabase import SupabaseIdentity
    from app.models.user import User

    now = datetime.now(timezone.utc)
    user = User(
        id="aware-last-login-user",
        email="aware@example.com",
        display_name="Ada Lovelace",
        supabase_user_id="aware-last-login-subject",
        auth_provider="google",
        is_active=True,
        last_login_at=now,
    )
    identity = SupabaseIdentity(
        subject="aware-last-login-subject",
        email="aware@example.com",
        display_name="Ada Lovelace",
        provider="google",
    )
    monkeypatch.setattr(auth_service, "_by_subject", lambda _db, _subject: user)

    resolved = auth_service.resolve_supabase_user(db_session, identity)

    assert resolved is user
    assert user.last_login_at == now


def test_password_session_reports_password_sign_in_method(client, account):
    session = client.get("/api/v1/auth/session", headers=account["headers"])
    assert session.status_code == 200
    assert session.json()["sign_in_method"] == "password"


# --------------------------------------------------------------------------- #
# 5-6. Application-user creation and de-duplication
# --------------------------------------------------------------------------- #
def test_first_google_login_creates_the_application_user(client, supabase_auth):
    email = f"first-{uuid.uuid4().hex[:8]}@example.com"
    subject = str(uuid.uuid4())
    token = supabase_token(subject=subject, email=email, display_name="Ada Lovelace")

    response = client.get("/api/v1/auth/session", headers=google_headers(token))
    assert response.status_code == 200
    app_user_id = response.json()["user"]["id"]

    rows = _user_rows(email)
    assert len(rows) == 1
    user = rows[0]
    assert user.id == app_user_id
    assert user.supabase_user_id == subject
    assert user.auth_provider == "google"
    assert user.password_hash is None
    assert user.avatar_url == "https://example.test/avatar.png"
    assert user.last_login_at is not None


def test_repeated_google_login_does_not_duplicate_the_user(client, supabase_auth):
    email = f"repeat-{uuid.uuid4().hex[:8]}@example.com"
    subject = str(uuid.uuid4())
    token = supabase_token(subject=subject, email=email)

    first = client.get("/api/v1/auth/session", headers=google_headers(token))
    second = client.get("/api/v1/auth/session", headers=google_headers(token))
    assert first.status_code == second.status_code == 200
    assert first.json()["user"]["id"] == second.json()["user"]["id"]
    assert len(_user_rows(email)) == 1


def test_a_changed_email_still_resolves_through_the_supabase_subject(client, supabase_auth):
    subject = str(uuid.uuid4())
    original = f"before-{uuid.uuid4().hex[:8]}@example.com"
    renamed = f"after-{uuid.uuid4().hex[:8]}@example.com"

    first = client.get("/api/v1/auth/session", headers=google_headers(supabase_token(subject=subject, email=original)))
    assert first.status_code == 200
    # The renewal token carries the same subject with a new address. The subject,
    # not the email, is the identity, so the account must not be duplicated.
    second = client.get("/api/v1/auth/session", headers=google_headers(supabase_token(subject=subject, email=renamed)))
    assert second.status_code == 200
    assert second.json()["user"]["id"] == first.json()["user"]["id"]


def test_password_account_with_the_same_email_is_not_silently_linked(client, supabase_auth):
    email = f"password-owner-{uuid.uuid4().hex[:8]}@example.com"
    registered = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": DEFAULT_PASSWORD, "display_name": "Password Owner"},
    )
    assert registered.status_code == 201
    original_id = registered.json()["user"]["id"]

    response = client.get("/api/v1/auth/session", headers=google_headers(supabase_token(email=email)))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_in_use"

    # The password account is untouched and still signs in normally.
    still_password = client.post("/api/v1/auth/login", json={"email": email, "password": DEFAULT_PASSWORD})
    assert still_password.status_code == 200
    assert still_password.json()["user"]["id"] == original_id
    rows = _user_rows(email)
    assert len(rows) == 1 and rows[0].supabase_user_id is None


def test_deactivated_account_cannot_sign_in_with_google(client, supabase_auth, db_session):
    from sqlalchemy import select

    from app.models.user import User

    subject = str(uuid.uuid4())
    email = f"deactivated-{uuid.uuid4().hex[:8]}@example.com"
    token = supabase_token(subject=subject, email=email)
    assert client.get("/api/v1/auth/session", headers=google_headers(token)).status_code == 200

    user = db_session.scalar(select(User).where(User.supabase_user_id == subject))
    user.is_active = False
    db_session.commit()

    blocked = client.get("/api/v1/auth/session", headers=google_headers(token))
    assert blocked.status_code == 401
    assert blocked.json()["error"]["code"] == "account_inactive"


# --------------------------------------------------------------------------- #
# 7-9. Ownership
# --------------------------------------------------------------------------- #
def test_user_can_access_their_own_workflow_and_run(client, supabase_auth, workflow_factory):
    email = f"owner-{uuid.uuid4().hex[:8]}@example.com"
    headers = google_headers(supabase_token(email=email))
    workflow = workflow_factory(headers, name="Owner workflow")

    listing = client.get("/api/v1/workflows", headers=headers)
    assert listing.status_code == 200
    assert any(item["id"] == workflow["id"] for item in listing.json()["items"])

    detail = client.get(f"/api/v1/workflows/{workflow['id']}", headers=headers)
    assert detail.status_code == 200

    started = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=headers)
    assert started.status_code == 201, started.text
    run = started.json()
    assert client.get(f"/api/v1/runs/{run['id']}", headers=headers).status_code == 200


def test_user_cannot_reach_another_users_workflow(client, supabase_auth, workflow_factory):
    headers_a = google_headers(supabase_token(email=f"a-{uuid.uuid4().hex[:8]}@example.com"))
    headers_b = google_headers(supabase_token(email=f"b-{uuid.uuid4().hex[:8]}@example.com"))
    workflow = workflow_factory(headers_a, name="Private workflow")

    assert client.get(f"/api/v1/workflows/{workflow['id']}", headers=headers_b).status_code == 404
    assert client.patch(
        f"/api/v1/workflows/{workflow['id']}", json={"name": "Hijacked"}, headers=headers_b
    ).status_code == 404
    assert client.delete(f"/api/v1/workflows/{workflow['id']}", headers=headers_b).status_code == 404
    assert client.post(
        f"/api/v1/workflows/{workflow['id']}/publish", json={"note": "x"}, headers=headers_b
    ).status_code == 404

    listing = client.get("/api/v1/workflows", headers=headers_b).json()
    assert all(item["id"] != workflow["id"] for item in listing["items"])


def test_user_cannot_reach_another_users_run(client, supabase_auth, workflow_factory):
    headers_a = google_headers(supabase_token(email=f"a-{uuid.uuid4().hex[:8]}@example.com"))
    headers_b = google_headers(supabase_token(email=f"b-{uuid.uuid4().hex[:8]}@example.com"))
    workflow = workflow_factory(headers_a, name="Run owner")
    started = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=headers_a)
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]

    assert client.get(f"/api/v1/runs/{run_id}", headers=headers_b).status_code == 404
    assert client.get(f"/api/v1/runs/{run_id}/events", headers=headers_b).status_code == 404
    assert client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers_b).status_code == 404
    assert client.delete(f"/api/v1/runs/{run_id}", headers=headers_b).status_code == 404


def test_user_cannot_reach_another_users_schedule(client, supabase_auth, workflow_factory):
    headers_a = google_headers(supabase_token(email=f"a-{uuid.uuid4().hex[:8]}@example.com"))
    headers_b = google_headers(supabase_token(email=f"b-{uuid.uuid4().hex[:8]}@example.com"))
    workflow = workflow_factory(headers_a, name="Schedule owner")
    created = client.post(
        f"/api/v1/workflows/{workflow['id']}/schedules",
        json={"cron_expression": "0 7 * * *", "timezone": "UTC", "name": "Mine"},
        headers=headers_a,
    )
    assert created.status_code == 201, created.text
    schedule_id = created.json()["id"]

    assert client.get(f"/api/v1/schedules/{schedule_id}", headers=headers_b).status_code == 404
    assert client.patch(
        f"/api/v1/schedules/{schedule_id}", json={"name": "Yours"}, headers=headers_b
    ).status_code == 404
    assert client.delete(f"/api/v1/schedules/{schedule_id}", headers=headers_b).status_code == 404


# --------------------------------------------------------------------------- #
# 10. Session lifetime and sign-out
# --------------------------------------------------------------------------- #
def test_session_expiry_and_sign_out_are_enforced(client, supabase_auth):
    subject = str(uuid.uuid4())
    email = f"lifetime-{uuid.uuid4().hex[:8]}@example.com"
    # Older than the tolerated clock skew, so the token is unambiguously expired.
    stale = supabase_token(subject=subject, email=email, expires_in=-120)
    response = client.get("/api/v1/auth/session", headers=google_headers(stale))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"

    # Signing out in the browser simply discards the token, so the next call
    # carries no credential at all.
    assert client.get("/api/v1/auth/session").status_code == 401

    # Signing in again mints a fresh, valid session for the same account.
    renewed = client.get(
        "/api/v1/auth/session", headers=google_headers(supabase_token(subject=subject, email=email))
    )
    assert renewed.status_code == 200

    short_lived = supabase_token(subject=subject, email=email, expires_in=1)
    assert client.get("/api/v1/auth/session", headers=google_headers(short_lived)).status_code == 200


def test_a_second_identity_cannot_claim_an_email_that_is_already_linked(client, supabase_auth):
    email = f"taken-{uuid.uuid4().hex[:8]}@example.com"
    first = supabase_token(subject=str(uuid.uuid4()), email=email)
    assert client.get("/api/v1/auth/session", headers=google_headers(first)).status_code == 200

    # A different Supabase user presenting the same address must not be mapped
    # onto the existing account (that would be an account takeover).
    impostor = supabase_token(subject=str(uuid.uuid4()), email=email)
    response = client.get("/api/v1/auth/session", headers=google_headers(impostor))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_in_use"
    assert len(_user_rows(email)) == 1


# --------------------------------------------------------------------------- #
# JWKS (asymmetric) verification path
# --------------------------------------------------------------------------- #
def _ec_public_jwk(kid: str) -> tuple[dict, object]:
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    numbers = private_key.public_key().public_numbers()

    def encode(value: int) -> str:
        length = (value.bit_length() + 7) // 8
        return _b64url(value.to_bytes(length, "big"))

    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": encode(numbers.x),
        "y": encode(numbers.y),
        "kid": kid,
        "alg": "ES256",
        "use": "sig",
    }
    return jwk, private_key


def test_asymmetric_tokens_are_verified_through_the_published_jwks(client, monkeypatch):
    kid = "test-signing-key-1"
    jwk, private_key = _ec_public_jwk(kid)

    for settings in _settings_objects():
        monkeypatch.setattr(settings, "supabase_url", SUPABASE_PROJECT_URL)
        # No shared secret: verification must go through the JWKS.
        monkeypatch.setattr(settings, "supabase_jwt_secret", "")
        monkeypatch.setattr(settings, "supabase_jwt_audience", "authenticated")
        monkeypatch.setattr(settings, "supabase_jwks_url", "")

    fetched: list[str] = []

    def fake_download(url: str) -> dict:
        fetched.append(url)
        return {"keys": [jwk]}

    monkeypatch.setattr(supabase_module, "_download_jwks", fake_download)
    supabase_module.clear_jwks_cache()

    now = int(time.time())
    email = f"asymmetric-{uuid.uuid4().hex[:8]}@example.com"
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "aud": "authenticated",
            "iss": SUPABASE_ISSUER,
            "iat": now,
            "exp": now + 3600,
            "email": email,
            "role": "authenticated",
            "user_metadata": {"full_name": "Key Pair"},
            "app_metadata": {"provider": "google"},
        },
        private_key,
        algorithm="ES256",
        headers={"kid": kid},
    )

    first = client.get("/api/v1/auth/session", headers=google_headers(token))
    assert first.status_code == 200, first.text
    assert first.json()["user"]["email"] == email
    assert fetched == [f"{SUPABASE_PROJECT_URL}/auth/v1/.well-known/jwks.json"]

    # A second request reuses the cached key set rather than calling out again.
    assert client.get("/api/v1/auth/session", headers=google_headers(token)).status_code == 200
    assert len(fetched) == 1


def test_unknown_signing_key_is_rejected(client, monkeypatch):
    _, private_key = _ec_public_jwk("published-key")
    for settings in _settings_objects():
        monkeypatch.setattr(settings, "supabase_url", SUPABASE_PROJECT_URL)
        monkeypatch.setattr(settings, "supabase_jwt_secret", "")
    monkeypatch.setattr(
        supabase_module,
        "_download_jwks",
        lambda url: {"keys": [{"kty": "EC", "crv": "P-256", "x": "AAAA", "y": "AAAA", "kid": "published-key"}]},
    )
    supabase_module.clear_jwks_cache()

    now = int(time.time())
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "aud": "authenticated",
            "iss": SUPABASE_ISSUER,
            "iat": now,
            "exp": now + 3600,
            "email": "unknown-key@example.com",
            "role": "authenticated",
        },
        private_key,
        algorithm="ES256",
        headers={"kid": "a-key-that-is-not-published"},
    )
    assert client.get("/api/v1/workflows", headers=google_headers(token)).status_code == 401


def test_unreachable_jwks_endpoint_fails_closed(client, monkeypatch):
    import urllib.error

    for settings in _settings_objects():
        monkeypatch.setattr(settings, "supabase_url", SUPABASE_PROJECT_URL)
        monkeypatch.setattr(settings, "supabase_jwt_secret", "")

    def explode(request, timeout=None):
        raise urllib.error.URLError("no route to host")

    # Patch the transport, not the error handling, so the real fail-closed path
    # (URLError -> SupabaseTokenError -> 401) is what gets exercised.
    monkeypatch.setattr(supabase_module.urllib.request, "urlopen", explode)
    supabase_module.clear_jwks_cache()

    _, private_key = _ec_public_jwk("k")
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "aud": "authenticated",
            "iss": SUPABASE_ISSUER,
            "iat": now,
            "exp": now + 3600,
            "email": "offline@example.com",
            "role": "authenticated",
        },
        private_key,
        algorithm="ES256",
        headers={"kid": "k"},
    )
    # The failure must surface as 401, never as a 500 or an unauthenticated pass.
    assert client.get("/api/v1/workflows", headers=google_headers(token)).status_code == 401


# --------------------------------------------------------------------------- #
# Verification unit tests
# --------------------------------------------------------------------------- #
def test_verify_supabase_token_returns_the_typed_identity(supabase_auth):
    email = "unit@example.com"
    subject = str(uuid.uuid4())
    identity = supabase_module.verify_supabase_token(supabase_token(subject=subject, email=email))

    assert identity.subject == subject
    assert identity.email == email
    assert identity.display_name == "Ada Lovelace"
    assert identity.avatar_url == "https://example.test/avatar.png"
    assert identity.provider == "google"
    assert identity.is_anonymous is False


def test_verify_supabase_token_rejects_a_blank_token(supabase_auth):
    with pytest.raises(supabase_module.SupabaseTokenError) as error:
        supabase_module.verify_supabase_token("   ")
    assert error.value.code == "missing_token"


def test_verify_supabase_token_needs_a_secret_for_hs256(monkeypatch):
    for settings in _settings_objects():
        monkeypatch.setattr(settings, "supabase_url", SUPABASE_PROJECT_URL)
        monkeypatch.setattr(settings, "supabase_jwt_secret", "")
    with pytest.raises(supabase_module.SupabaseTokenError) as error:
        supabase_module.verify_supabase_token(supabase_token())
    assert error.value.code == "shared_secret_missing"


# --------------------------------------------------------------------------- #
# Existing credentials keep working
# --------------------------------------------------------------------------- #
def test_application_tokens_and_api_tokens_still_work(client, account, supabase_auth):
    assert client.get("/api/v1/auth/session", headers=account["headers"]).status_code == 200

    minted = client.post("/api/v1/auth/tokens", json={"name": "script", "scopes": ["read"]}, headers=account["headers"])
    assert minted.status_code == 201
    api_token = minted.json()["token"]
    assert client.get("/api/v1/auth/session", headers=google_headers(api_token)).status_code == 200

    revoked = client.delete(f"/api/v1/auth/tokens/{minted.json()['id']}", headers=account["headers"])
    assert revoked.status_code == 204
    assert client.get("/api/v1/auth/session", headers=google_headers(api_token)).status_code == 401


def test_google_account_and_password_account_are_separate_owners(client, account, supabase_auth, workflow_factory):
    password_workflow = workflow_factory(account["headers"], name="Password owner workflow")
    google_headers_ = google_headers(supabase_token(email=f"google-{uuid.uuid4().hex[:8]}@example.com"))

    assert client.get(f"/api/v1/workflows/{password_workflow['id']}", headers=google_headers_).status_code == 404
    assert client.get("/api/v1/workflows", headers=google_headers_).json()["total"] == 0
