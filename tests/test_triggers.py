"""Signed webhook delivery and workflow-success trigger behavior."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from sqlalchemy import select


def signed_post(client, trigger, secret: str, payload: dict, key: str, *, timestamp: int | None = None, signature: str | None = None):
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sent_at = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), str(sent_at).encode() + b"." + body, hashlib.sha256).hexdigest()
    return client.post(
        f"/api/v1/hooks/{trigger['id']}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Orchestrator-Timestamp": str(sent_at),
            "X-Orchestrator-Signature": signature or f"sha256={digest}",
            "Idempotency-Key": key,
        },
    )


def test_signed_webhook_maps_inputs_deduplicates_and_is_owner_scoped(client, account, other_account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    secret = "a" * 64
    created = client.post(
        f"/api/v1/workflows/{workflow['id']}/triggers",
        json={
            "name": "Order event",
            "kind": "webhook",
            "rate_limit_per_minute": 1,
            "input_mapping": {"order_id": "payload.data.id"},
            "signing_secret": secret,
        },
        headers=account["headers"],
    )
    assert created.status_code == 201, created.text
    trigger = created.json()
    assert "secret_once" not in trigger and trigger["version"] == workflow["version"]
    assert secret not in json.dumps(trigger)
    assert client.get(f"/api/v1/triggers/{trigger['id']}", headers=other_account["headers"]).status_code == 404

    payload = {"data": {"id": "order-123", "unused": "not copied"}}
    accepted = signed_post(client, trigger, secret, payload, "event-001")
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["replayed"] is False
    run_id = accepted.json()["run_id"]
    run = client.get(f"/api/v1/runs/{run_id}", headers=account["headers"]).json()
    assert run["input"] == {"order_id": "order-123"}
    assert run["trigger"] == "webhook"

    replay = signed_post(client, trigger, secret, payload, "event-001")
    assert replay.status_code == 202
    assert replay.json()["run_id"] == run_id
    assert replay.json()["replayed"] is True

    limited = signed_post(client, trigger, secret, {"data": {"id": "order-456"}}, "event-002")
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "trigger_rate_limited"

    stale = signed_post(client, trigger, secret, payload, "event-stale", timestamp=int(time.time()) - 600)
    assert stale.status_code == 401
    bad_signature = signed_post(client, trigger, secret, payload, "event-bad", signature="sha256=bad")
    assert bad_signature.status_code == 401


def test_webhook_secret_rotation_invalidates_the_old_secret(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    old_secret = "b" * 64
    response = client.post(
        f"/api/v1/workflows/{workflow['id']}/triggers",
        json={"name": "Rotating hook", "kind": "webhook", "signing_secret": old_secret},
        headers=account["headers"],
    )
    trigger = response.json()
    new_secret = "c" * 64
    rotated = client.post(f"/api/v1/triggers/{trigger['id']}/rotate-secret", json={"signing_secret": new_secret}, headers=account["headers"])
    assert rotated.status_code == 200
    assert "secret_once" not in rotated.json() and new_secret != old_secret

    rejected = signed_post(client, trigger, old_secret, {"value": 1}, "old-key")
    accepted = signed_post(client, trigger, new_secret, {"value": 1}, "new-key")
    assert rejected.status_code == 401
    assert accepted.status_code == 202
    assert new_secret not in json.dumps(client.get("/api/v1/triggers", headers=account["headers"]).json())


def test_workflow_success_triggers_start_a_mapped_run_and_reject_cycles(client, account, workflow_factory, db_session):
    source = workflow_factory(account["headers"], name="Trigger source")
    target = workflow_factory(account["headers"], name="Trigger target")
    created = client.post(
        f"/api/v1/workflows/{target['id']}/triggers",
        json={
            "name": "After source succeeds",
            "kind": "workflow_success",
            "source_workflow_id": source["id"],
            "input_mapping": {"upstream_run": "payload.run_id"},
        },
        headers=account["headers"],
    )
    assert created.status_code == 201, created.text

    reverse = client.post(
        f"/api/v1/workflows/{source['id']}/triggers",
        json={"name": "Cycle", "kind": "workflow_success", "source_workflow_id": target["id"]},
        headers=account["headers"],
    )
    assert reverse.status_code == 409
    assert reverse.json()["error"]["code"] == "trigger_cycle"

    from app.models.run import WorkflowRun
    from app.services import run_service
    from app.services import workflow_service

    source_row = workflow_service.get_workflow(db_session, source["id"], account["user"]["id"])
    run, _ = run_service.create_run(db_session, source_row, input_data={"seed": True})
    run.status = "succeeded"
    run.output_data = {"result": "ok"}
    run_service._finish_run(db_session, run, "succeeded")
    db_session.commit()

    target_run = db_session.scalar(
        select(WorkflowRun).where(
            WorkflowRun.workflow_id == target["id"], WorkflowRun.trigger == "workflow_success"
        )
    )
    assert target_run is not None
    assert target_run.input_data == {"upstream_run": run.id}


def test_demo_seed_installs_webhook_pipeline_idempotently(client, account, db_session):
    from app.models.workflow import WorkflowTrigger
    from app.services.demo_data import install_demo_webhook_trigger, install_demo_workflows

    workflows = install_demo_workflows(db_session, account["user"]["id"])
    assert any(workflow.name == "Webhook order intake" for workflow in workflows)
    trigger = install_demo_webhook_trigger(db_session, account["user"]["id"])
    db_session.commit()
    same_trigger = install_demo_webhook_trigger(db_session, account["user"]["id"])
    db_session.commit()
    assert trigger.id == same_trigger.id
    assert db_session.scalar(select(WorkflowTrigger).where(WorkflowTrigger.id == trigger.id)) is not None

    listed = client.get("/api/v1/triggers", headers=account["headers"]).json()
    seeded = next(item for item in listed["items"] if item["id"] == trigger.id)
    assert seeded["workflow_name"] == "Webhook order intake"
    assert seeded["input_mapping"] == {"order_id": "payload.data.id", "customer": "payload.data.customer"}
    assert "secret_once" not in seeded
