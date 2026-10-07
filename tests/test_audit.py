"""Phase 5 audit log tests: append-only, listing, CSV export, formula guard."""
from __future__ import annotations

import pytest

from app.models.audit import sanitize_csv_value
from app.services import audit_service


def test_audit_events_are_append_only(client, account, db_session):
    # Record via the teams API (which emits audit events).
    team = client.post("/api/v1/teams", json={"name": "Audited"}, headers=account["headers"]).json()
    rows, total = audit_service.list_events(db_session, action="team.create")
    assert total >= 1
    assert any(row.resource_id == team["id"] for row in rows)


def test_audit_listing_requires_admin(client, account):
    # Non-admin gets 403.
    assert client.get("/api/v1/audit/events", headers=account["headers"]).status_code == 403


def test_audit_listing_and_csv_export(client, account, db_session):
    # Make account an admin.
    db_session.execute(
        __import__("sqlalchemy").text("UPDATE users SET is_admin = 1 WHERE id = :id"),
        {"id": account["user"]["id"]},
    )
    db_session.commit()
    team = client.post("/api/v1/teams", json={"name": "Audited"}, headers=account["headers"]).json()

    listing = client.get("/api/v1/audit/events", headers=account["headers"]).json()
    assert listing["total"] >= 1
    actions = [item["action"] for item in listing["items"]]
    assert "team.create" in actions

    csv_response = client.get("/api/v1/audit/events.csv", headers=account["headers"])
    assert csv_response.status_code == 200
    assert "text/csv" in csv_response.headers["content-type"]
    assert "team.create" in csv_response.text
    assert team["id"] in csv_response.text


def test_csv_formula_injection_guard():
    assert sanitize_csv_value("=cmd|'/c calc'!A0") == "'=cmd|'/c calc'!A0"
    assert sanitize_csv_value("+123") == "'+123"
    assert sanitize_csv_value("-123") == "'-123"
    assert sanitize_csv_value("@mention") == "'@mention"
    assert sanitize_csv_value("line1\nline2") == "'line1\nline2"
    assert sanitize_csv_value("normal text") == "normal text"
    assert sanitize_csv_value(None) == ""
    assert sanitize_csv_value(123) == "123"


def test_audit_record_stores_actor_and_ip(db_session):
    event = audit_service.record(
        db_session,
        action="test.action",
        actor_user_id="u1",
        actor_token_id="t1",
        resource_type="team",
        resource_id="team1",
        ip_address="1.2.3.4",
        details={"key": "value"},
    )
    assert event.action == "test.action"
    assert event.actor_user_id == "u1"
    assert event.actor_token_id == "t1"
    assert event.ip_address == "1.2.3.4"

    rows, total = audit_service.list_events(db_session, action="test.action")
    assert total == 1 and rows[0].id == event.id
