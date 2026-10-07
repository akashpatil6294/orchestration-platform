"""Task-type catalog for the visual builder (Stage H, H1).

GET /api/v1/task-types exposes every registered handler with the builder
metadata the palette and inspector render from: category grouping,
input_schema-driven fields, idempotency flags for the retry-policy warning,
and default_policy recommendations.
"""
from __future__ import annotations


def test_task_types_requires_auth(client):
    response = client.get("/api/v1/task-types")
    assert response.status_code in (401, 403), response.text


def test_task_types_lists_all_handlers(client, account):
    response = client.get("/api/v1/task-types", headers=account["headers"])
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["count"] == 21
    assert len(payload["items"]) == 21
    assert set(payload["categories"]) == {"ai", "connector", "demo", "document", "storage", "transform"}


def test_task_types_every_handler_has_builder_metadata(client, account):
    response = client.get("/api/v1/task-types", headers=account["headers"])
    assert response.status_code == 200, response.text
    for item in response.json()["items"]:
        assert item["task_type"], "task_type must be set"
        assert item["description"], f"{item['task_type']}: description must be set"
        assert item["category"], f"{item['task_type']}: category must be set"
        assert isinstance(item["input_schema"], dict), f"{item['task_type']}: input_schema must be a dict"
        assert item["input_schema"].get("type") == "object", f"{item['task_type']}: input_schema must be an object schema"
        assert isinstance(item["idempotent"], bool), f"{item['task_type']}: idempotent must be a bool"
        assert isinstance(item["default_policy"], dict), f"{item['task_type']}: default_policy must be a dict"


def test_task_types_idempotency_is_honest(client, account):
    """Side-effecting outbound calls are not idempotent; pure transforms are."""
    response = client.get("/api/v1/task-types", headers=account["headers"])
    assert response.status_code == 200, response.text
    by_type = {item["task_type"]: item for item in response.json()["items"]}
    for task_type in ("http.request", "webhook.call", "slack.post", "email.send"):
        assert by_type[task_type]["idempotent"] is False, task_type
        assert by_type[task_type]["side_effects"] is True, task_type
    for task_type in ("sql.query", "transform.json", "storage.put", "ai.summarize", "document.extract_text"):
        assert by_type[task_type]["idempotent"] is True, task_type


def test_task_types_ai_extract_schema_requires_json_schema(client, account):
    response = client.get("/api/v1/task-types", headers=account["headers"])
    assert response.status_code == 200, response.text
    by_type = {item["task_type"]: item for item in response.json()["items"]}
    schema = by_type["ai.extract"]["input_schema"]
    assert "json_schema" in schema.get("required", [])


def test_task_types_sorted_by_type(client, account):
    response = client.get("/api/v1/task-types", headers=account["headers"])
    assert response.status_code == 200, response.text
    types = [item["task_type"] for item in response.json()["items"]]
    assert types == sorted(types)
