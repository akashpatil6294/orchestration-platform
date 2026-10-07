"""Phase 5 quota tests: 429 bodies carry limit, usage and reset."""
from __future__ import annotations

import pytest

from app.config import settings
from app.services import quota_service


def _start(client, headers, workflow_id):
    return client.post(f"/api/v1/workflows/{workflow_id}/runs", json={}, headers=headers)


def test_concurrent_quota_returns_429_with_details(client, account, workflow_factory, monkeypatch):
    monkeypatch.setattr(settings, "quota_concurrent_runs", 1)
    monkeypatch.setattr(settings, "quota_runs_per_day", 0)
    workflow = workflow_factory(account["headers"])

    first = _start(client, account["headers"], workflow["id"])
    assert first.status_code == 201, first.text

    second = _start(client, account["headers"], workflow["id"])
    assert second.status_code == 429
    body = second.json()["error"]
    assert body["code"] == "quota_exceeded"
    assert body["details"]["quota"] == "concurrent_runs"
    assert body["details"]["limit"] == 1
    assert body["details"]["usage"] == 1
    assert "reset" in body["details"]


def test_daily_quota_returns_429_with_reset(client, account, workflow_factory, monkeypatch):
    monkeypatch.setattr(settings, "quota_runs_per_day", 1)
    monkeypatch.setattr(settings, "quota_concurrent_runs", 0)
    workflow = workflow_factory(account["headers"])

    first = _start(client, account["headers"], workflow["id"])
    assert first.status_code == 201, first.text

    second = _start(client, account["headers"], workflow["id"])
    assert second.status_code == 429
    body = second.json()["error"]
    assert body["code"] == "quota_exceeded"
    details = body["details"]
    assert details["quota"] == "runs_per_day"
    assert details["limit"] == 1
    assert details["usage"] == 1
    assert details["reset"]  # ISO timestamp when the window rolls over


def test_quota_disabled_when_zero(client, account, workflow_factory, monkeypatch):
    monkeypatch.setattr(settings, "quota_runs_per_day", 0)
    monkeypatch.setattr(settings, "quota_concurrent_runs", 0)
    workflow = workflow_factory(account["headers"])
    for _ in range(3):
        response = _start(client, account["headers"], workflow["id"])
        assert response.status_code == 201, response.text
