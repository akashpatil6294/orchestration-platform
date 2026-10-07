"""Authentication, authorisation and ownership tests."""
from __future__ import annotations

import pytest


def test_health_and_readiness(client, worker_client):
    assert client.get("/health").json()["status"] == "ok"
    ready = client.get("/ready")
    assert ready.status_code == 200
    payload = ready.json()
    assert payload["status"] == "ready"
    assert payload["workers"]["active"] >= 1


def test_ready_reports_degraded_when_no_worker_is_available(client, db_session):
    from app.models.worker import Worker

    db_session.query(Worker).delete()
    db_session.commit()
    ready = client.get("/ready")
    assert ready.status_code == 200
    payload = ready.json()
    assert payload["status"] == "degraded"
    assert payload["workers"]["active"] == 0


def test_metrics_endpoint_renders_prometheus_text(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "orchestrator_uptime_seconds" in response.text
    assert response.headers["content-type"].startswith("text/plain")


def test_register_login_and_session(client):
    register = client.post(
        "/api/v1/auth/register",
        json={"email": "login-test@example.com", "password": "testpassword123", "display_name": "Login Test"},
    )
    assert register.status_code == 201
    assert register.json()["user"]["email"] == "login-test@example.com"

    login = client.post("/api/v1/auth/login", json={"email": "login-test@example.com", "password": "testpassword123"})
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    session = client.get("/api/v1/auth/session", headers=headers)
    assert session.status_code == 200
    assert session.json()["user"]["email"] == "login-test@example.com"
    assert "workflows:write" in session.json()["permissions"]


def test_duplicate_email_is_rejected(client):
    payload = {"email": "dupe@example.com", "password": "testpassword123"}
    assert client.post("/api/v1/auth/register", json=payload).status_code == 201
    again = client.post("/api/v1/auth/register", json=payload)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "email_taken"


def test_weak_password_is_rejected(client):
    response = client.post("/api/v1/auth/register", json={"email": "weak@example.com", "password": "allletters"})
    assert response.status_code == 422


def test_wrong_password_does_not_reveal_account_existence(client, account):
    wrong_password = client.post("/api/v1/auth/login", json={"email": account["email"], "password": "wrongpassword1"})
    unknown_user = client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "wrongpassword1"})
    assert wrong_password.status_code == unknown_user.status_code == 401
    assert wrong_password.json()["error"]["message"] == unknown_user.json()["error"]["message"]


def test_protected_routes_require_a_token(client):
    for method, path in (("get", "/api/v1/workflows"), ("get", "/api/v1/runs"), ("get", "/api/v1/runs/dashboard")):
        response = getattr(client, method)(path)
        assert response.status_code == 401, path


def test_invalid_token_is_rejected(client):
    response = client.get("/api/v1/workflows", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_workflows_are_isolated_between_accounts(client, account, other_account, workflow_factory):
    workflow = workflow_factory(account["headers"], name="Private workflow")
    assert client.get(f"/api/v1/workflows/{workflow['id']}", headers=account["headers"]).status_code == 200
    hidden = client.get(f"/api/v1/workflows/{workflow['id']}", headers=other_account["headers"])
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "workflow_not_found"
    listing = client.get("/api/v1/workflows", headers=other_account["headers"]).json()
    assert listing["items"] == []


def test_other_account_cannot_start_or_read_runs(client, account, other_account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    run = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"input": {}}, headers=account["headers"])
    assert run.status_code == 201
    run_id = run.json()["id"]
    assert client.get(f"/api/v1/runs/{run_id}", headers=other_account["headers"]).status_code == 404
    assert client.post(f"/api/v1/runs/{run_id}/cancel", headers=other_account["headers"]).status_code == 404
    assert client.get(f"/api/v1/runs/{run_id}/events", headers=other_account["headers"]).status_code == 404


def test_api_tokens_work_and_can_be_revoked(client, account):
    created = client.post("/api/v1/auth/tokens", json={"name": "ci", "scopes": ["read"]}, headers=account["headers"])
    assert created.status_code == 201
    token = created.json()["token"]
    assert token.startswith("orch_")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/workflows", headers=headers).status_code == 200
    assert client.delete(f"/api/v1/auth/tokens/{created.json()['id']}", headers=account["headers"]).status_code == 204
    assert client.get("/api/v1/workflows", headers=headers).status_code == 401


def test_worker_routes_reject_user_credentials(client, account):
    response = client.post(
        "/api/v1/workers/claim",
        json={"worker_id": "w1", "available_slots": 1},
        headers=account["headers"],
    )
    assert response.status_code == 401
    # A user session token is never accepted as a worker credential.
    assert response.json()["error"]["code"] in {"missing_worker_token", "invalid_worker_token"}


def test_worker_routes_reject_missing_credentials(client):
    response = client.post("/api/v1/workers/claim", json={"worker_id": "w1", "available_slots": 1})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_worker_token"


def test_user_routes_reject_worker_credentials(worker_client, account):
    response = worker_client["client"].get("/api/v1/workflows")
    assert response.status_code == 401


def test_worker_credentials_are_hashed_not_stored(worker_client, db_session):
    from app.models.worker import Worker

    worker = db_session.get(Worker, worker_client["worker_id"])
    assert worker is not None
    assert worker_client["token"] not in worker.token_hash
    assert len(worker.token_hash) == 64


def test_worker_registration_is_idempotent(worker_client):
    client = worker_client["client"]
    first = client.post(
        "/api/v1/workers/register",
        json={"worker_id": worker_client["worker_id"], "task_types": ["demo.echo"], "max_concurrency": 2},
    )
    second = client.post(
        "/api/v1/workers/register",
        json={"worker_id": worker_client["worker_id"], "task_types": ["demo.echo", "demo.add"], "max_concurrency": 2},
    )
    assert first.status_code == second.status_code == 201
    assert second.json()["task_types"] == ["demo.add", "demo.echo"]
    assert second.json()["heartbeat_interval_seconds"] > 0


def test_oversized_request_body_is_rejected(client, account):
    big = {"name": "x" * 200, "description": "y" * 5000, "steps": [{"id": "a", "type": "demo.echo", "input": {"blob": "z" * 200_000}}]}
    response = client.post("/api/v1/workflows", json=big, headers=account["headers"])
    assert response.status_code in (413, 422)


def test_validation_error_shape_is_consistent(client, account):
    response = client.post("/api/v1/workflows", json={"name": "", "steps": []}, headers=account["headers"])
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert isinstance(body["error"]["details"], list)
