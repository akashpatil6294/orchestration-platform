"""Phase 5 team tests: creation, membership roles, isolation."""
from __future__ import annotations

import pytest

from app.core.errors import Invalid, NotFound
from app.models.team import role_at_least
from app.services import team_service


def _user_id(account):
    return account["user"]["id"]


def test_create_team_makes_creator_admin(client, account, db_session):
    response = client.post("/api/v1/teams", json={"name": "Engineering"}, headers=account["headers"])
    assert response.status_code == 201, response.text
    team = response.json()
    assert team["name"] == "Engineering"

    member = team_service.membership(db_session, team["id"], _user_id(account))
    assert member is not None and member.role == "admin"


def test_list_teams_returns_membership_roles(client, account):
    created = client.post("/api/v1/teams", json={"name": "Data"}, headers=account["headers"]).json()
    listing = client.get("/api/v1/teams", headers=account["headers"]).json()
    mine = [item for item in listing["items"] if item["id"] == created["id"]]
    assert len(mine) == 1 and mine[0]["role"] == "admin"


def test_admin_can_add_member_with_role(client, account, other_account, db_session):
    team = client.post("/api/v1/teams", json={"name": "Ops"}, headers=account["headers"]).json()
    response = client.put(
        f"/api/v1/teams/{team['id']}/members",
        json={"user_id": _user_id(other_account), "role": "editor"},
        headers=account["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "editor"

    member = team_service.membership(db_session, team["id"], _user_id(other_account))
    assert member.role == "editor"


def test_non_admin_cannot_add_member(client, account, other_account):
    team = client.post("/api/v1/teams", json={"name": "Ops"}, headers=account["headers"]).json()
    # other_account is not a member at all -> 404 (no existence leak)
    response = client.put(
        f"/api/v1/teams/{team['id']}/members",
        json={"user_id": _user_id(account), "role": "viewer"},
        headers=other_account["headers"],
    )
    assert response.status_code == 404


def test_viewer_cannot_add_member(client, account, other_account):
    team = client.post("/api/v1/teams", json={"name": "Ops"}, headers=account["headers"]).json()
    client.put(
        f"/api/v1/teams/{team['id']}/members",
        json={"user_id": _user_id(other_account), "role": "viewer"},
        headers=account["headers"],
    )
    third = client.post(
        "/api/v1/auth/register",
        json={"email": "third-teams@example.com", "password": "testpassword123", "display_name": "Third"},
    )
    third_headers = {"Authorization": f"Bearer {third.json()['access_token']}"}
    response = client.put(
        f"/api/v1/teams/{team['id']}/members",
        json={"user_id": third.json()["user"]["id"], "role": "viewer"},
        headers=other_account["headers"],
    )
    assert response.status_code == 404


def test_invalid_role_is_rejected(client, account, other_account):
    team = client.post("/api/v1/teams", json={"name": "Ops"}, headers=account["headers"]).json()
    response = client.put(
        f"/api/v1/teams/{team['id']}/members",
        json={"user_id": _user_id(other_account), "role": "superuser"},
        headers=account["headers"],
    )
    assert response.status_code == 422  # pydantic pattern guard


def test_admin_can_remove_member(client, account, other_account, db_session):
    team = client.post("/api/v1/teams", json={"name": "Ops"}, headers=account["headers"]).json()
    client.put(
        f"/api/v1/teams/{team['id']}/members",
        json={"user_id": _user_id(other_account), "role": "viewer"},
        headers=account["headers"],
    )
    response = client.delete(f"/api/v1/teams/{team['id']}/members/{_user_id(other_account)}", headers=account["headers"])
    assert response.status_code == 204
    assert team_service.membership(db_session, team["id"], _user_id(other_account)) is None


def test_last_admin_cannot_be_removed(client, account):
    team = client.post("/api/v1/teams", json={"name": "Solo"}, headers=account["headers"]).json()
    response = client.delete(f"/api/v1/teams/{team['id']}/members/{_user_id(account)}", headers=account["headers"])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "team_last_admin"


def test_non_member_cannot_see_team(client, account, other_account):
    team = client.post("/api/v1/teams", json={"name": "Secret"}, headers=account["headers"]).json()
    assert client.get(f"/api/v1/teams/{team['id']}", headers=other_account["headers"]).status_code == 404
    assert client.get(f"/api/v1/teams/{team['id']}/members", headers=other_account["headers"]).status_code == 404


def test_role_levels_are_ordered():
    assert role_at_least("admin", "viewer")
    assert role_at_least("operator", "editor")
    assert role_at_least("editor", "editor")
    assert not role_at_least("viewer", "editor")
    assert not role_at_least("editor", "admin")
