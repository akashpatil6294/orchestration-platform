"""Draft optimistic concurrency (Stage H, H1).

PATCH /api/v1/workflows/{id} requires If-Match: <draft_version>. A stale
version returns 409 with the current draft and version so the builder can
show a reload-or-overwrite dialog instead of silently clobbering edits.
"""
from __future__ import annotations


def _create_workflow(client, headers):
    response = client.post(
        "/api/v1/workflows",
        headers=headers,
        json={"name": "Concurrency test", "description": "", "steps": [{"id": "a", "type": "demo.echo"}]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_patch_without_if_match_still_works(client, account):
    """If-Match is optional; omitting it keeps the old autosave behaviour."""
    workflow = _create_workflow(client, account["headers"])
    workflow_id = workflow["id"]
    response = client.patch(
        f"/api/v1/workflows/{workflow_id}",
        headers=account["headers"],
        json={"description": "no if-match"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["draft_version"] == workflow["draft_version"] + 1


def test_patch_with_matching_if_match_succeeds(client, account):
    workflow = _create_workflow(client, account["headers"])
    workflow_id = workflow["id"]
    version = workflow["draft_version"]
    headers = {**account["headers"], "If-Match": str(version)}
    response = client.patch(
        f"/api/v1/workflows/{workflow_id}",
        headers=headers,
        json={"description": "matching version"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["draft_version"] == version + 1


def test_patch_with_stale_if_match_returns_409(client, account):
    workflow = _create_workflow(client, account["headers"])
    workflow_id = workflow["id"]
    # Another editor saves first, bumping the version.
    first = client.patch(
        f"/api/v1/workflows/{workflow_id}",
        headers={**account["headers"], "If-Match": str(workflow["draft_version"])},
        json={"description": "first editor"},
    )
    assert first.status_code == 200
    new_version = first.json()["draft_version"]
    assert new_version == workflow["draft_version"] + 1
    # Stale editor tries with the old version.
    stale = client.patch(
        f"/api/v1/workflows/{workflow_id}",
        headers={**account["headers"], "If-Match": str(workflow["draft_version"])},
        json={"description": "stale editor"},
    )
    assert stale.status_code == 409, stale.text
    body = stale.json()
    assert body["error"]["code"] == "draft_version_conflict"
    details = body["error"]["details"]
    assert details["current_version"] == new_version
    assert details["expected_version"] == workflow["draft_version"]
    assert "current_draft" in details
    # The stale write did not land.
    get = client.get(f"/api/v1/workflows/{workflow_id}", headers=account["headers"])
    assert get.json()["draft"]["description"] == "first editor"


def test_patch_after_conflict_can_overwrite_with_fresh_version(client, account):
    workflow = _create_workflow(client, account["headers"])
    workflow_id = workflow["id"]
    first = client.patch(
        f"/api/v1/workflows/{workflow_id}",
        headers=account["headers"],
        json={"description": "first"},
    )
    fresh_version = first.json()["draft_version"]
    retry = client.patch(
        f"/api/v1/workflows/{workflow_id}",
        headers={**account["headers"], "If-Match": str(fresh_version)},
        json={"description": "overwrite after reload"},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["draft"]["description"] == "overwrite after reload"


def test_migration_backfills_draft_version(app_module):
    """The H1 migration gives every existing workflow draft_version=1."""
    from app.database import SessionLocal
    from app.models.workflow import Workflow

    with SessionLocal() as db:
        row = db.query(Workflow.draft_version).first()
        # Either no workflows exist yet or every one has a version.
        if row is not None:
            assert all(version >= 1 for (version,) in db.query(Workflow.draft_version).all())
