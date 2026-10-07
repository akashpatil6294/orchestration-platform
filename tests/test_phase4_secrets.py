"""Phase 4 secret tests: {{secrets.NAME}} interpolation and end-to-end leak
proofs — a secret value must never appear in any persisted row, API response
or log line."""
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.core.security import encrypt_secret, hash_password
from app.database import SessionLocal
from app.models.run import StepRun, WorkflowRun
from app.models.user import User
from app.models.workflow import Workflow, WorkflowSecret
from app.services.workflow_service import referenced_secret_names, resolve_secrets


def _seed_user_and_workflow() -> tuple[User, Workflow]:
    with SessionLocal() as db:
        email = f"sec-{uuid.uuid4().hex[:10]}@example.com"
        user = User(email=email, display_name="Sec", password_hash=hash_password("testpassword123"), is_admin=False)
        db.add(user)
        db.flush()
        workflow = Workflow(owner_id=user.id, name="Secret workflow", description="", draft={"name": "Secret workflow", "steps": []}, default_max_parallel=2)
        db.add(workflow)
        db.flush()
        db.add(WorkflowSecret(workflow_id=workflow.id, name="API_KEY", ciphertext=encrypt_secret("super-secret-value-9876"), created_at=__import__("datetime").datetime.utcnow(), updated_at=__import__("datetime").datetime.utcnow()))
        db.add(WorkflowSecret(workflow_id=workflow.id, name="SLACK_TOKEN", ciphertext=encrypt_secret("xoxb-another-secret-1357"), created_at=__import__("datetime").datetime.utcnow(), updated_at=__import__("datetime").datetime.utcnow()))
        db.commit()
        return user, workflow


def test_template_references_are_discovered_and_resolved(app_module):
    definition = {
        "url": "https://hooks.example/{{secrets. SLACK_TOKEN}}".replace(" ", ""),
        "nested": {"connection_url": "{{secrets.API_KEY}}"},
        "mixed": "prefix-{{secrets.API_KEY}}-suffix",
        "plain": "no secrets here",
    }
    user, workflow = _seed_user_and_workflow()
    names = referenced_secret_names(definition)
    assert names == {"SLACK_TOKEN", "API_KEY"}

    with SessionLocal() as db:
        resolved, paths = resolve_secrets(db, workflow.id, definition)
    assert resolved["url"] == "https://hooks.example/xoxb-another-secret-1357"
    assert resolved["nested"]["connection_url"] == "super-secret-value-9876"
    assert resolved["mixed"] == "prefix-super-secret-value-9876-suffix"
    assert resolved["plain"] == "no secrets here"
    assert set(paths) == {"url", "nested.connection_url", "mixed"}


def test_unknown_template_secret_is_rejected_before_dispatch(app_module):
    definition = {"token": "{{secrets.MISSING}}"}
    user, workflow = _seed_user_and_workflow()
    with SessionLocal() as db:
        import pytest
        from app.core.errors import Invalid

        with pytest.raises(Invalid, match="MISSING"):
            resolve_secrets(db, workflow.id, definition)


def test_secret_value_never_appears_in_persisted_rows_responses_or_logs(client, account, worker_client, workflow_factory):
    secret_value = "wrk-e2e-leak-check-2468"

    # Store the secret through the API as the account owner.
    workflow = workflow_factory(
        account["headers"],
        {
            "name": "Leak check",
            "steps": [
                {"id": "leak", "type": "demo.echo", "input": {"token": "{{secrets.LEAKED}}"}, "depends_on": [], "timeout_seconds": 60},
            ],
        },
    )
    stored = client.put(
        f"/api/v1/workflows/{workflow['id']}/secrets/LEAKED",
        json={"name": "LEAKED", "value": secret_value},
        headers=account["headers"],
    )
    assert stored.status_code == 200, stored.text
    assert secret_value not in stored.text

    # Secret listings never include values.
    listing = client.get(f"/api/v1/workflows/{workflow['id']}/secrets", headers=account["headers"]).json()
    assert all("value" not in item for item in listing)

    # Run the workflow and echo the secret back through the handler output.
    started = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"])
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    for _ in range(30):
        claimed = worker_client["client"].post(
            "/api/v1/workers/claim",
            json={"worker_id": worker_client["worker_id"], "available_slots": 4, "task_types": ["demo.echo"]},
        )
        tasks = claimed.json().get("tasks") or []
        if not tasks:
            detail = client.get(f"/api/v1/runs/{run_id}", headers=account["headers"]).json()
            if detail["status"] in {"succeeded", "failed"}:
                break
            continue
        for task in tasks:
            # The worker genuinely received the plaintext (dispatch-time
            # resolution) and echoes it straight back into its output and logs.
            assert task["input"]["token"] == secret_value
            client.post(
                f"/api/v1/tasks/{task['id']}/complete",
                json={
                    "worker_id": worker_client["worker_id"],
                    "lease_token": task["lease_token"],
                    "output": {"value": task["input"]["token"], "label": "leak"},
                    "logs": [{"level": "info", "message": f"used {task['input']['token']} at least once"}],
                },
            )

    # Every persisted/read surface must be redacted.
    detail = client.get(f"/api/v1/runs/{run_id}", headers=account["headers"])
    assert detail.status_code == 200
    assert secret_value not in detail.text
    events = client.get(f"/api/v1/runs/{run_id}/events", headers=account["headers"])
    assert secret_value not in events.text

    with SessionLocal() as db:
        step_rows = list(db.scalars(select(StepRun).where(StepRun.run_id == run_id)).all())
        run_row = db.get(WorkflowRun, run_id)
    dump = f"{[row.__dict__ for row in step_rows]}{run_row.__dict__}"
    assert secret_value not in dump, "the secret value leaked into a persisted row"
    for row in step_rows:
        assert secret_value not in json_dumps_safe(row.logs)
        assert secret_value not in json_dumps_safe(row.output_data)


def json_dumps_safe(value) -> str:
    import json

    return json.dumps(value, default=str)
