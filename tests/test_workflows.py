"""Workflow draft, validation, publishing, versioning and secret tests."""
from __future__ import annotations

from app.services import demo_data


def test_summarize_tolerates_a_cyclic_draft():
    """A draft may be mid-edit; the summary must never raise."""
    from app.core import dag

    cyclic = {"name": "wip", "steps": [{"id": "a", "type": "demo.echo", "depends_on": ["b"]}, {"id": "b", "type": "demo.echo", "depends_on": ["a"]}]}
    summary = dag.summarize_definition(cyclic)
    assert summary["step_count"] == 2
    assert summary["has_cycle"] is True
    assert summary["order"] == []


def simple_definition(name="Test workflow", **overrides):
    payload = {
        "name": name,
        "description": "A workflow used in tests",
        "default_max_parallel": 3,
        "steps": [
            {"id": "start", "type": "demo.echo", "input": {"value": "hello"}, "depends_on": []},
            {"id": "left", "type": "demo.add", "input": {"values": [1, 2]}, "depends_on": ["start"]},
            {"id": "right", "type": "demo.echo", "input": {"value": "world"}, "depends_on": ["start"]},
            {"id": "join", "type": "demo.summarize", "input": {}, "depends_on": ["left", "right"]},
        ],
    }
    payload.update(overrides)
    return payload


def test_create_list_and_fetch_workflow(client, account):
    created = client.post("/api/v1/workflows", json=simple_definition(), headers=account["headers"])
    assert created.status_code == 201, created.text
    workflow = created.json()
    assert workflow["latest_version"] == 0
    assert workflow["step_count"] == 4
    assert workflow["has_draft_changes"] is True

    listing = client.get("/api/v1/workflows", headers=account["headers"]).json()
    assert listing["total"] == 1
    assert listing["items"][0]["name"] == "Test workflow"

    detail = client.get(f"/api/v1/workflows/{workflow['id']}", headers=account["headers"]).json()
    assert detail["draft"]["steps"][0]["id"] == "start"
    assert detail["versions"] == []


def test_search_and_filter_workflows(client, account, workflow_factory):
    workflow_factory(account["headers"], name="Alpha pipeline")
    workflow_factory(account["headers"], name="Beta pipeline")
    found = client.get("/api/v1/workflows", params={"search": "alpha"}, headers=account["headers"]).json()
    assert [item["name"] for item in found["items"]] == ["Alpha pipeline"]
    assert client.get("/api/v1/workflows", params={"search": "nothing-matches"}, headers=account["headers"]).json()["items"] == []


def test_invalid_draft_is_rejected_on_create(client, account):
    broken = simple_definition()
    broken["steps"].append({"id": "loop", "type": "demo.echo", "input": {}, "depends_on": ["join"]})
    broken["steps"][0]["depends_on"] = ["loop"]
    response = client.post("/api/v1/workflows", json=broken, headers=account["headers"])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_workflow"
    assert any("cycle" in issue["message"] for issue in response.json()["error"]["details"])


def test_draft_can_be_saved_with_a_cycle_but_not_published(client, account, workflow_factory):
    """A draft is a workspace: it may be incomplete, publishing may not be."""
    workflow = workflow_factory(account["headers"], name="Draft freedom")
    patched = client.patch(
        f"/api/v1/workflows/{workflow['id']}",
        json={"steps": [{"id": "a", "type": "demo.echo", "input": {}, "depends_on": ["b"]}, {"id": "b", "type": "demo.echo", "input": {}, "depends_on": ["a"]}]},
        headers=account["headers"],
    )
    assert patched.status_code == 200
    assert patched.json()["has_draft_changes"] is True

    validated = client.post(f"/api/v1/workflows/{workflow['id']}/validate", json={}, headers=account["headers"]).json()
    assert validated["valid"] is False
    assert any(issue["code"] == "graph.cycle" for issue in validated["errors"])

    publish = client.post(f"/api/v1/workflows/{workflow['id']}/publish", json={"note": "nope"}, headers=account["headers"])
    assert publish.status_code == 422
    assert publish.json()["error"]["code"] == "workflow_invalid"


def test_validate_accepts_a_candidate_definition(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    candidate = simple_definition(name="Candidate", steps=[{"id": "only", "type": "demo.echo", "input": {}}])
    result = client.post(
        f"/api/v1/workflows/{workflow['id']}/validate",
        json={"definition": candidate},
        headers=account["headers"],
    ).json()
    assert result["valid"] is True
    assert result["summary"]["step_count"] == 1


def test_validation_returns_warnings_for_risky_shapes(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    parallel_only = simple_definition(steps=[{"id": "a", "type": "demo.echo", "input": {}}, {"id": "b", "type": "demo.echo", "input": {}}])
    result = client.post(f"/api/v1/workflows/{workflow['id']}/validate", json={"definition": parallel_only}, headers=account["headers"]).json()
    assert result["valid"] is True
    assert any(issue["code"] == "graph.no_edges" for issue in result["warnings"])


def test_publishing_creates_immutable_versions(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"], publish=False)
    first = client.post(f"/api/v1/workflows/{workflow['id']}/publish", json={"note": "first"}, headers=account["headers"])
    assert first.status_code == 200
    assert first.json()["version"] == 1

    # Change the draft and publish again.
    client.patch(
        f"/api/v1/workflows/{workflow['id']}",
        json={"description": "changed after v1"},
        headers=account["headers"],
    )
    second = client.post(f"/api/v1/workflows/{workflow['id']}/publish", json={"note": "second"}, headers=account["headers"])
    assert second.json()["version"] == 2

    versions = client.get(f"/api/v1/workflows/{workflow['id']}/versions", headers=account["headers"]).json()
    assert [item["version"] for item in versions] == [2, 1]
    assert versions[0]["is_latest"] is True
    assert versions[0]["publish_note"] == "second"

    v1 = client.get(f"/api/v1/workflows/{workflow['id']}/versions/1", headers=account["headers"]).json()
    assert v1["definition"]["description"] == demo_data.DEMO_SMOKE_DEFINITION["description"]
    assert v1["is_latest"] is False


def test_publishing_twice_without_changes_is_still_a_new_version(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    again = client.post(f"/api/v1/workflows/{workflow['id']}/publish", json={"note": "republish"}, headers=account["headers"])
    assert again.json()["version"] == 2


def test_secrets_are_write_only(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    created = client.put(
        f"/api/v1/workflows/{workflow['id']}/secrets/api_key",
        json={"name": "api_key", "value": "super-secret-value"},
        headers=account["headers"],
    )
    assert created.status_code == 200
    assert "value" not in created.json()

    listed = client.get(f"/api/v1/workflows/{workflow['id']}/secrets", headers=account["headers"]).json()
    assert [item["name"] for item in listed] == ["api_key"]
    assert "super-secret-value" not in str(listed)

    # The definition may reference the secret by name only.
    patched = client.patch(
        f"/api/v1/workflows/{workflow['id']}",
        json={"steps": [{"id": "use", "type": "demo.echo", "input": {"value": {"$secret": "api_key"}}, "depends_on": []}]},
        headers=account["headers"],
    )
    assert patched.status_code == 200
    assert client.post(f"/api/v1/workflows/{workflow['id']}/validate", json={}, headers=account["headers"]).json()["valid"] is True

    assert client.delete(f"/api/v1/workflows/{workflow['id']}/secrets/api_key", headers=account["headers"]).status_code == 204


def test_validation_flags_missing_secret_reference(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    client.patch(
        f"/api/v1/workflows/{workflow['id']}",
        json={"steps": [{"id": "use", "type": "demo.echo", "input": {"token": {"$secret": "not_defined"}}, "depends_on": []}]},
        headers=account["headers"],
    )
    result = client.post(f"/api/v1/workflows/{workflow['id']}/validate", json={}, headers=account["headers"]).json()
    assert result["valid"] is False
    assert any(issue["code"] == "secret.missing" for issue in result["errors"])


def test_workflow_activity_stream_records_publication(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    activity = client.get(f"/api/v1/workflows/{workflow['id']}/activity", headers=account["headers"]).json()
    assert any(item["type"] == "workflow.published" for item in activity)


def test_archive_hides_workflow_from_default_listing(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    archived = client.post(f"/api/v1/workflows/{workflow['id']}/archive", headers=account["headers"])
    assert archived.status_code == 200
    assert archived.json()["archived"] is True
    assert client.get("/api/v1/workflows", headers=account["headers"]).json()["items"] == []
    with_archived = client.get("/api/v1/workflows", params={"include_archived": True}, headers=account["headers"]).json()
    assert len(with_archived["items"]) == 1


def test_delete_workflow_removes_it(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    assert client.delete(f"/api/v1/workflows/{workflow['id']}", headers=account["headers"]).status_code == 204
    assert client.get(f"/api/v1/workflows/{workflow['id']}", headers=account["headers"]).status_code == 404


def test_demo_workflow_definition_is_valid():
    from app.core import dag

    assert dag.validate_definition(demo_data.DEMO_DEFINITION) == []

    summary = dag.summarize_definition(demo_data.DEMO_DEFINITION)
    # Parallel branches at level 0.
    levels = dag.levels(dag.definition_edges(demo_data.DEMO_DEFINITION))
    assert levels["fetch_primary"] == levels["fetch_secondary"] == 0
    assert levels["aggregate"] == 1
    assert levels["publish"] == levels["notify"] == 3
    assert summary["step_count"] == 6

    retrying = [step for step in demo_data.DEMO_DEFINITION["steps"] if step["retries"] > 0]
    assert retrying, "the sample workflow must demonstrate a retry"
