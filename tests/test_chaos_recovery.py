"""Chaos injection and recovery endpoints (Stage H, H5)."""
from __future__ import annotations

import os


def test_chaos_config_defaults_off():
    from app.worker.chaos import ChaosConfig

    config = ChaosConfig()
    assert config.enabled is False
    assert config.describe() == "disabled"


def test_chaos_config_from_env(monkeypatch):
    from app.worker.chaos import ChaosConfig

    monkeypatch.setenv("CHAOS_FAILURE_RATE", "0.5")
    monkeypatch.setenv("CHAOS_FAIL_STEPS", "fetch, transform")
    config = ChaosConfig()
    assert config.enabled is True
    assert config.failure_rate == 0.5
    assert config.fail_steps == {"fetch", "transform"}


def test_chaos_fail_step_raises(monkeypatch):
    from app.worker import chaos as chaos_module
    from app.worker.chaos import ChaosConfig, ChaosError, maybe_inject

    monkeypatch.setenv("CHAOS_FAIL_STEPS", "fetch")
    config = ChaosConfig()
    try:
        maybe_inject(config, "fetch")
        raise AssertionError("should have raised")
    except ChaosError as exc:
        assert "fetch" in str(exc)
    # Other steps are unaffected.
    maybe_inject(config, "transform")


def test_chaos_slow(monkeypatch):
    import time

    from app.worker.chaos import ChaosConfig, maybe_inject

    monkeypatch.setenv("CHAOS_SLOW_SECONDS", "0.05")
    config = ChaosConfig()
    start = time.perf_counter()
    maybe_inject(config, "any")
    assert time.perf_counter() - start >= 0.04


def test_recovery_summary_empty(client, account):
    response = client.get("/api/v1/ops/recovery/summary", headers=account["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["crashed_runs"] == 0
    assert body["dlq_depth"] == 0


def test_recovery_crashed_lists_lease_expired(client, account):
    from app import database as db_module
    from app.models.run import StepRun, WorkflowRun

    wf = client.post(
        "/api/v1/workflows",
        headers=account["headers"],
        json={"name": "crash wf", "steps": [{"id": "a", "type": "demo.echo"}]},
    )
    workflow_id = wf.json()["id"]
    user_id = account["user"]["id"]

    with db_module.SessionLocal() as db:
        run = WorkflowRun(workflow_id=workflow_id, version=1, status="running", trigger="manual")
        db.add(run)
        db.flush()
        db.add(StepRun(run_id=run.id, step_key="a", task_type="demo.echo", status="lease_expired"))
        db.commit()
        run_id = run.id

    crashed = client.get("/api/v1/ops/recovery/crashed", headers=account["headers"])
    assert crashed.status_code == 200, crashed.text
    items = crashed.json()["items"]
    assert any(i["run_id"] == run_id for i in items)

    summary = client.get("/api/v1/ops/recovery/summary", headers=account["headers"]).json()
    assert summary["crashed_runs"] >= 1


def test_recovery_requires_auth(client):
    response = client.get("/api/v1/ops/recovery/summary")
    assert response.status_code == 401
