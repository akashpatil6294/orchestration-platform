"""Alert rules engine and cost guard (Stage H, H4)."""
from __future__ import annotations


def test_create_rule_and_list(client, account):
    response = client.post(
        "/api/v1/alerts/rules",
        headers=account["headers"],
        json={"name": "fail watcher", "condition": "run_failed", "params": {}},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["condition"] == "run_failed"

    listing = client.get("/api/v1/alerts/rules", headers=account["headers"])
    assert listing.status_code == 200
    assert any(r["id"] == body["id"] for r in listing.json()["items"])
    assert "run_failed" in listing.json()["conditions"]


def test_create_rule_unknown_condition_rejected(client, account):
    response = client.post(
        "/api/v1/alerts/rules",
        headers=account["headers"],
        json={"name": "bad", "condition": "explode", "params": {}},
    )
    assert response.status_code == 422, response.text


def test_rule_fires_on_run_failed_with_cooldown(client, account, app_module):
    from app.services import alert_service

    # Create a workflow and a failed run via the service layer.
    wf = client.post(
        "/api/v1/workflows",
        headers=account["headers"],
        json={"name": "alert wf", "steps": [{"id": "a", "type": "demo.echo"}]},
    )
    assert wf.status_code == 201
    workflow_id = wf.json()["id"]
    client.post(f"/api/v1/workflows/{workflow_id}/publish", headers=account["headers"], json={"note": "v1"})

    rule = client.post(
        "/api/v1/alerts/rules",
        headers=account["headers"],
        json={"name": "run fail", "workflow_id": workflow_id, "condition": "run_failed",
              "params": {}, "cooldown_seconds": 3600},
    ).json()

    # Simulate: fetch the rule and evaluate a failed run directly.
    from app import database as db_module
    from app.models.alert import AlertRule
    from app.models.run import StepRun, WorkflowRun

    with db_module.SessionLocal() as db:
        db_rule = db.get(AlertRule, rule["id"])
        run = WorkflowRun(workflow_id=workflow_id,
                          version=1, status="failed", trigger="manual")
        db.add(run)
        db.flush()
        step = StepRun(run_id=run.id, step_key="a", task_type="demo.echo", status="failed")
        db.add(step)
        db.flush()
        fired = alert_service.evaluate_step_event(db, step, run)
        db.commit()
        our_fired = [e for e in fired if e.rule_id == rule["id"]]
        assert len(our_fired) == 1
        assert our_fired[0].condition == "run_failed"

        # Second evaluation within cooldown fires nothing for our rule.
        fired2 = alert_service.evaluate_step_event(db, step, run)
        assert not [e for e in fired2 if e.rule_id == rule["id"]]


def _fake_step(key, status):
    from app.models.run import StepRun
    return StepRun(id=f"s-{key}", run_id="r1", step_key=key, status=status, task_type="demo.echo")


def test_dry_run_rule(client, account):
    # Use a workflow-scoped rule to avoid cross-test interference.
    wf = client.post(
        "/api/v1/workflows",
        headers=account["headers"],
        json={"name": "dry run wf", "steps": [{"id": "a", "type": "demo.echo"}]},
    )
    workflow_id = wf.json()["id"]
    rule = client.post(
        "/api/v1/alerts/rules",
        headers=account["headers"],
        json={"name": "quiet check", "workflow_id": workflow_id,
              "condition": "no_successful_run", "params": {"minutes": 60}},
    ).json()
    response = client.post(f"/api/v1/alerts/rules/{rule['id']}/test", headers=account["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    # No successful runs exist for this workflow, so it would fire.
    assert body["would_fire"] is True
    assert body["in_cooldown"] is False


def test_budget_warn_and_hard_stop(client, account):
    wf = client.post(
        "/api/v1/workflows",
        headers=account["headers"],
        json={"name": "budget wf", "steps": [{"id": "a", "type": "demo.echo"}]},
    )
    workflow_id = wf.json()["id"]
    client.post(f"/api/v1/workflows/{workflow_id}/publish", headers=account["headers"], json={"note": "v1"})

    # Set a tiny budget.
    budget = client.put(
        "/api/v1/alerts/budgets",
        headers=account["headers"],
        json={"workflow_id": workflow_id, "monthly_budget_usd": 10.0,
              "warn_at_pct": 80.0, "hard_stop_at_pct": 100.0},
    )
    assert budget.status_code == 200, budget.text
    assert budget.json()["monthly_budget_usd"] == 10.0

    # Simulate spend via the service layer: steps record ai_usage.cost_usd.
    from app import database as db_module
    from app.models.run import StepRun, WorkflowRun
    from app.services import alert_service

    with db_module.SessionLocal() as db:
        user_id = account["user"]["id"]
        expensive = WorkflowRun(workflow_id=workflow_id,  version=1,
                                status="succeeded", trigger="manual")
        db.add(expensive)
        db.flush()
        db.add(StepRun(run_id=expensive.id, step_key="a", task_type="demo.echo",
                       status="succeeded",
                       output_data={"ai_usage": {"cost_usd": 9.0}}))
        db.commit()
        fired = alert_service.check_budgets(db)
        db.commit()
        warnings = [e for e in fired if e.condition == "budget_warning" and e.workflow_id == workflow_id]
        assert len(warnings) == 1
        assert warnings[0].workflow_id == workflow_id

        # Push over 100%: hard stop cancels queued runs.
        expensive2 = WorkflowRun(workflow_id=workflow_id,  version=1,
                                 status="succeeded", trigger="manual")
        db.add(expensive2)
        db.flush()
        db.add(StepRun(run_id=expensive2.id, step_key="a", task_type="demo.echo",
                       status="succeeded",
                       output_data={"ai_usage": {"cost_usd": 5.0}}))
        queued = WorkflowRun(workflow_id=workflow_id,  version=1,
                             status="queued", trigger="manual")
        db.add(queued)
        db.commit()
        fired = alert_service.check_budgets(db)
        db.commit()
        stops = [e for e in fired if e.condition == "budget_hard_stop" and e.workflow_id == workflow_id]
        assert len(stops) == 1
        db.refresh(queued)
        assert queued.status == "cancelled"


def test_alert_ack_flow(client, account):
    rule = client.post(
        "/api/v1/alerts/rules",
        headers=account["headers"],
        json={"name": "ack me", "condition": "no_successful_run", "params": {"minutes": 1},
              "cooldown_seconds": 60},
    ).json()
    # Force a fire via dry sweep.
    from app import database as db_module
    from app.models.alert import AlertRule
    from app.services import alert_service

    with db_module.SessionLocal() as db:
        db_rule = db.get(AlertRule, rule["id"])
        fired = alert_service.sweep_time_based_rules(db)
        db.commit()
        assert any(e.rule_id == rule["id"] for e in fired)

    alerts = client.get("/api/v1/alerts", headers=account["headers"]).json()["items"]
    target = next(e for e in alerts if e["rule_id"] == rule["id"])
    assert target["acknowledged_at"] is None

    ack = client.post(f"/api/v1/alerts/{target['id']}/ack", headers=account["headers"])
    assert ack.status_code == 200
    assert ack.json()["acknowledged_at"] is not None

    # Filter by acknowledged.
    filtered = client.get("/api/v1/alerts?acknowledged=true", headers=account["headers"]).json()["items"]
    assert any(e["id"] == target["id"] for e in filtered)
