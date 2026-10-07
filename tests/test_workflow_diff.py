"""Workflow definition diff (Stage H, H1).

GET /workflows/{id}/diff compares two definitions structurally: steps
added/removed/changed (with per-field changes), edge changes, and
workflow-level changes. The builder's publish flow renders this.
"""
from __future__ import annotations


def _create_workflow(client, headers):
    response = client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "name": "Diff workflow",
            "description": "",
            "steps": [
                {"id": "a", "type": "demo.echo", "input": {"value": "hello"}},
                {"id": "b", "type": "demo.echo", "input": {"value": "world"}, "depends_on": ["a"]},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_diff_draft_against_itself_has_no_changes(client, account):
    workflow = _create_workflow(client, account["headers"])
    response = client.get(
        f"/api/v1/workflows/{workflow['id']}/diff",
        headers=account["headers"],
        params={"from_version": "draft", "to_version": "draft"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_changes"] is False
    assert body["steps_added"] == []
    assert body["steps_removed"] == []
    assert body["steps_changed"] == []


def test_diff_detects_added_removed_and_changed_steps(client, account):
    workflow = _create_workflow(client, account["headers"])
    workflow_id = workflow["id"]
    # Publish v1.
    pub = client.post(f"/api/v1/workflows/{workflow_id}/publish", headers=account["headers"], json={"note": "v1"})
    assert pub.status_code == 200, pub.text
    # Edit the draft: change a's input, drop b, add c with a new edge.
    patch = client.patch(
        f"/api/v1/workflows/{workflow_id}",
        headers=account["headers"],
        json={
            "steps": [
                {"id": "a", "type": "demo.echo", "input": {"value": "changed"}},
                {"id": "c", "type": "demo.add", "input": {"numbers": [1]}, "depends_on": ["a"]},
            ]
        },
    )
    assert patch.status_code == 200, patch.text
    response = client.get(
        f"/api/v1/workflows/{workflow_id}/diff",
        headers=account["headers"],
        params={"from_version": "version:1", "to_version": "draft"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["has_changes"] is True
    assert body["steps_added"] == ["c"]
    assert body["steps_removed"] == ["b"]
    assert len(body["steps_changed"]) == 1
    assert body["steps_changed"][0]["step_id"] == "a"
    changed_fields = {change["field"] for change in body["steps_changed"][0]["fields"]}
    assert "input" in changed_fields


def test_diff_detects_edge_changes(client, account):
    workflow = _create_workflow(client, account["headers"])
    workflow_id = workflow["id"]
    pub = client.post(f"/api/v1/workflows/{workflow_id}/publish", headers=account["headers"], json={"note": "v1"})
    assert pub.status_code == 200, pub.text
    patch = client.patch(
        f"/api/v1/workflows/{workflow_id}",
        headers=account["headers"],
        json={
            "steps": [
                {"id": "a", "type": "demo.echo", "input": {"value": "hello"}},
                {"id": "b", "type": "demo.echo", "input": {"value": "world"}, "depends_on": []},
            ]
        },
    )
    assert patch.status_code == 200, patch.text
    response = client.get(
        f"/api/v1/workflows/{workflow_id}/diff",
        headers=account["headers"],
        params={"from_version": "version:1", "to_version": "draft"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["edge_changes"]) == 1
    assert body["edge_changes"][0]["step_id"] == "b"
    assert body["edge_changes"][0]["removed_edges"] == ["a"]


def test_diff_bad_version_spec_422s(client, account):
    workflow = _create_workflow(client, account["headers"])
    response = client.get(
        f"/api/v1/workflows/{workflow['id']}/diff",
        headers=account["headers"],
        params={"from_version": "bogus", "to_version": "draft"},
    )
    assert response.status_code == 422, response.text


def test_diff_unknown_version_404s(client, account):
    workflow = _create_workflow(client, account["headers"])
    response = client.get(
        f"/api/v1/workflows/{workflow['id']}/diff",
        headers=account["headers"],
        params={"from_version": "version:99", "to_version": "draft"},
    )
    assert response.status_code == 404, response.text
