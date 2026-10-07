"""Phase 5 API-token scope tests: enforcement and route coverage."""
from __future__ import annotations

import pytest

from app.core.scopes import MANAGE, READ, RUN, SCOPES, required_scope_for, scope_covers


def _token_headers(client, account, scopes):
    created = client.post(
        "/api/v1/auth/tokens",
        json={"name": "scoped", "scopes": scopes},
        headers=account["headers"],
    )
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_scope_hierarchy():
    assert scope_covers(["manage"], MANAGE)
    assert scope_covers(["manage"], RUN)
    assert scope_covers(["manage"], READ)
    assert scope_covers(["run"], RUN)
    assert scope_covers(["run"], READ)
    assert not scope_covers(["run"], MANAGE)
    assert scope_covers(["read"], READ)
    assert not scope_covers(["read"], RUN)
    assert not scope_covers([], READ)


def test_read_token_can_list_but_not_create(client, account, workflow_factory):
    headers = _token_headers(client, account, ["read"])
    # Read: allowed
    assert client.get("/api/v1/workflows", headers=headers).status_code == 200
    # Create: needs run/manage -> 403
    response = client.post("/api/v1/workflows", json={"name": "x", "steps": [{"id": "s1", "type": "demo.echo", "input": {"v": 1}}]}, headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"


def test_run_token_can_create_but_not_delete(client, account, workflow_factory):
    headers = _token_headers(client, account, ["run"])
    created = client.post("/api/v1/workflows", json={"name": "x", "steps": [{"id": "s1", "type": "demo.echo", "input": {"v": 1}}]}, headers=headers)
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    # Delete: needs manage -> 403
    response = client.delete(f"/api/v1/workflows/{workflow_id}", headers=headers)
    assert response.status_code == 403


def test_manage_token_has_full_access(client, account):
    headers = _token_headers(client, account, ["manage"])
    created = client.post("/api/v1/workflows", json={"name": "x", "steps": [{"id": "s1", "type": "demo.echo", "input": {"v": 1}}]}, headers=headers)
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    assert client.get(f"/api/v1/workflows/{workflow_id}", headers=headers).status_code == 200
    assert client.delete(f"/api/v1/workflows/{workflow_id}", headers=headers).status_code in (200, 204)


def test_session_jwt_ignores_scopes(client, account):
    # Interactive sessions always carry full access.
    assert client.get("/api/v1/workflows", headers=account["headers"]).status_code == 200
    created = client.post("/api/v1/workflows", json={"name": "x", "steps": [{"id": "s1", "type": "demo.echo", "input": {"v": 1}}]}, headers=account["headers"])
    assert created.status_code == 201, created.text


def test_every_route_resolves_a_valid_scope():
    """Every registered user route must resolve to a known scope."""
    from app.main import create_app

    app = create_app()
    problems = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/v1/"):
            continue
        # Worker and auth-callback routes use their own credentials.
        if path.startswith("/api/v1/workers/") or path.startswith("/api/v1/auth/callback"):
            continue
        try:
            scope = required_scope_for(route)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{path}: {exc}")
            continue
        if scope not in SCOPES:
            problems.append(f"{path}: unknown scope {scope!r}")
    assert not problems, "routes without a valid scope:\n" + "\n".join(problems)
