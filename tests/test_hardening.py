"""Phase 5 hardening tests: auth rate limits and JSON depth limits."""
from __future__ import annotations

import json

import pytest

from app.core.rate_limit import RateLimiter, auth_limiter


def test_rate_limiter_sliding_window():
    limiter = RateLimiter(max_hits=3, window_seconds=60)
    assert limiter.allow("k")[0]
    assert limiter.allow("k")[0]
    assert limiter.allow("k")[0]
    allowed, retry_after = limiter.allow("k")
    assert not allowed
    assert retry_after >= 1
    # Different key is unaffected.
    assert limiter.allow("other")[0]


def test_auth_rate_limit_returns_429(client, monkeypatch):
    # Shrink the limiter for the test.
    limiter = auth_limiter()
    monkeypatch.setattr(limiter, "max_hits", 2)
    limiter.reset()
    try:
        for _ in range(2):
            response = client.post(
                "/api/v1/auth/login", json={"email": "x@example.com", "password": "wrongpassword1"}
            )
            assert response.status_code in (401, 422)
        limited = client.post(
            "/api/v1/auth/login", json={"email": "x@example.com", "password": "wrongpassword1"}
        )
        assert limited.status_code == 429
        body = limited.json()["error"]
        assert body["code"] == "auth_rate_limited"
        assert body["details"]["retry_after_seconds"] >= 1
    finally:
        limiter.reset()
        monkeypatch.setattr(limiter, "max_hits", 30)


def test_json_depth_limit_rejects_deep_payloads(client, account, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "max_json_depth", 8)
    # Build a payload nested deeper than the limit.
    payload: dict = {"v": 1}
    for _ in range(20):
        payload = {"nested": payload}
    response = client.post("/api/v1/workflows", json={"name": "deep", "steps": [{"id": "s1", "type": "demo.echo", "input": payload}]}, headers=account["headers"])
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_deep"


def test_json_depth_limit_allows_normal_payloads(client, account):
    response = client.post(
        "/api/v1/workflows",
        json={"name": "ok", "steps": [{"id": "s1", "type": "demo.echo", "input": {"a": {"b": [1, 2]}}}]},
        headers=account["headers"],
    )
    assert response.status_code == 201, response.text
