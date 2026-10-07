"""Phase 5 cross-tenant tests: team-scoped workflow/run access with roles."""
from __future__ import annotations

import pytest


def _register(client, email):
    response = client.post("/api/v1/auth/register", json={"email": email, "password": "secret1234"})
    assert response.status_code == 201, response.text
    data = response.json()
    token = data.get("access_token") or data.get("token")
    return {"Authorization": f"Bearer {token}"}, data["user"]["id"]


def _workflow(client, headers):
    response = client.post(
        "/api/v1/workflows",
        json={"name": "team-wf", "steps": [{"id": "s1", "type": "demo.echo", "input": {"v": 1}}]},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_team_member_can_read_but_viewer_cannot_write(client):
    owner_headers, owner_id = _register(client, "owner5@example.com")
    member_headers, member_id = _register(client, "member5@example.com")
    outsider_headers, _ = _register(client, "outsider5@example.com")

    team = client.post("/api/v1/teams", json={"name": "T5"}, headers=owner_headers).json()
    workflow = _workflow(client, owner_headers)

    # Assign workflow to the team.
    client.patch(
        f"/api/v1/workflows/{workflow['id']}",
        json={"team_id": team["id"]},
        headers=owner_headers,
    )

    # Add member as viewer.
    assert (
        client.put(
            f"/api/v1/teams/{team['id']}/members",
            json={"user_id": member_id, "role": "viewer"},
            headers=owner_headers,
        ).status_code
        == 200
    )

    # Viewer can read.
    assert client.get(f"/api/v1/workflows/{workflow['id']}", headers=member_headers).status_code == 200
    assert client.get("/api/v1/workflows", headers=member_headers).json()["total"] >= 1

    # Viewer cannot write.
    assert (
        client.patch(
            f"/api/v1/workflows/{workflow['id']}", json={"name": "hacked"}, headers=member_headers
        ).status_code
        == 403
    )
    # Viewer cannot start runs.
    assert (
        client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={}, headers=member_headers).status_code
        == 403
    )

    # Outsider sees nothing.
    assert client.get(f"/api/v1/workflows/{workflow['id']}", headers=outsider_headers).status_code == 404
    assert client.get("/api/v1/workflows", headers=outsider_headers).json()["total"] == 0


def test_editor_can_write_but_not_delete(client):
    owner_headers, owner_id = _register(client, "owner6@example.com")
    editor_headers, editor_id = _register(client, "editor6@example.com")

    team = client.post("/api/v1/teams", json={"name": "T6"}, headers=owner_headers).json()
    workflow = _workflow(client, owner_headers)
    client.patch(
        f"/api/v1/workflows/{workflow['id']}", json={"team_id": team["id"]}, headers=owner_headers
    )
    assert (
        client.put(
            f"/api/v1/teams/{team['id']}/members",
            json={"user_id": editor_id, "role": "editor"},
            headers=owner_headers,
        ).status_code
        == 200
    )

    # Editor can write.
    assert (
        client.patch(
            f"/api/v1/workflows/{workflow['id']}", json={"name": "edited"}, headers=editor_headers
        ).status_code
        == 200
    )
    # Editor cannot delete (admin only).
    assert (
        client.delete(f"/api/v1/workflows/{workflow['id']}", headers=editor_headers).status_code == 403
    )


def test_operator_can_start_runs(client):
    owner_headers, _ = _register(client, "owner7@example.com")
    op_headers, op_id = _register(client, "op7@example.com")

    team = client.post("/api/v1/teams", json={"name": "T7"}, headers=owner_headers).json()
    workflow = _workflow(client, owner_headers)
    client.patch(
        f"/api/v1/workflows/{workflow['id']}", json={"team_id": team["id"]}, headers=owner_headers
    )
    assert (
        client.put(
            f"/api/v1/teams/{team['id']}/members",
            json={"user_id": op_id, "role": "operator"},
            headers=owner_headers,
        ).status_code
        == 200
    )

    # Publish, then operator can start runs; operator outranks editor so it can
    # also edit, but cannot delete (admin only).
    assert (
        client.post(f"/api/v1/workflows/{workflow['id']}/publish", headers=owner_headers).status_code
        == 200
    )
    assert (
        client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={}, headers=op_headers).status_code
        == 201
    )
    assert client.delete(f"/api/v1/workflows/{workflow['id']}", headers=op_headers).status_code == 403
