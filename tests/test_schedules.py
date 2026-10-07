"""Recurring schedule tests: CRUD, cron preview, duplicate protection, overlap."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def schedule_payload(**overrides):
    payload = {"cron_expression": "0 7 * * *", "timezone": "UTC", "name": "Morning report", "enabled": True, "input": {"env": "prod"}}
    payload.update(overrides)
    return payload


def test_cron_preview_validates_and_lists_next_runs(client, account):
    ok = client.post("/api/v1/schedules/preview", json={"cron_expression": "*/30 * * * *", "timezone": "UTC", "count": 3}, headers=account["headers"])
    assert ok.status_code == 200
    body = ok.json()
    assert body["valid"] is True
    assert len(body["next_runs"]) == 3
    assert body["next_runs"] == sorted(body["next_runs"])

    bad = client.post("/api/v1/schedules/preview", json={"cron_expression": "not a cron", "timezone": "UTC"}, headers=account["headers"])
    assert bad.json()["valid"] is False
    assert "five fields" in bad.json()["error"]

    tz = client.post("/api/v1/schedules/preview", json={"cron_expression": "0 9 * * *", "timezone": "Not/AZone"}, headers=account["headers"])
    assert tz.json()["valid"] is False
    assert "timezone" in tz.json()["error"].lower()


def test_create_read_update_toggle_and_delete_schedule(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    created = client.post(f"/api/v1/workflows/{workflow['id']}/schedules", json=schedule_payload(), headers=account["headers"])
    assert created.status_code == 201, created.text
    schedule = created.json()
    assert schedule["workflow_id"] == workflow["id"]
    assert schedule["workflow_name"] == workflow["name"]
    assert schedule["next_run_at"] is not None
    assert schedule["effective_version"] == workflow["version"]
    assert schedule["cron_description"] == "Daily at 07:00"

    listed = client.get("/api/v1/schedules", headers=account["headers"]).json()
    assert listed["total"] == 1
    assert listed["stats"]["enabled"] == 1

    updated = client.patch(f"/api/v1/schedules/{schedule['id']}", json={"cron_expression": "15 6 * * mon-fri", "timezone": "Europe/Berlin"}, headers=account["headers"])
    assert updated.status_code == 200
    assert updated.json()["timezone"] == "Europe/Berlin"
    assert updated.json()["next_run_at"] != schedule["next_run_at"]

    paused = client.post(f"/api/v1/schedules/{schedule['id']}/toggle", json={"enabled": False}, headers=account["headers"]).json()
    assert paused["enabled"] is False
    assert paused["next_run_at"] is None

    resumed = client.post(f"/api/v1/schedules/{schedule['id']}/toggle", json={"enabled": True}, headers=account["headers"]).json()
    assert resumed["enabled"] is True
    assert resumed["next_run_at"] is not None

    assert client.delete(f"/api/v1/schedules/{schedule['id']}", headers=account["headers"]).status_code == 204
    assert client.get("/api/v1/schedules", headers=account["headers"]).json()["total"] == 0


def test_duplicate_schedule_is_rejected(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    first = client.post(f"/api/v1/workflows/{workflow['id']}/schedules", json=schedule_payload(), headers=account["headers"])
    assert first.status_code == 201
    again = client.post(f"/api/v1/workflows/{workflow['id']}/schedules", json=schedule_payload(), headers=account["headers"])
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "duplicate_schedule"


def test_invalid_cron_is_rejected_on_create(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    response = client.post(f"/api/v1/workflows/{workflow['id']}/schedules", json=schedule_payload(cron_expression="99 99 * * *"), headers=account["headers"])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_cron"


def test_schedule_requires_a_published_version(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"], publish=False)
    response = client.post(f"/api/v1/workflows/{workflow['id']}/schedules", json=schedule_payload(), headers=account["headers"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_published_version"


def test_schedules_are_isolated_between_accounts(client, account, other_account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    schedule = client.post(f"/api/v1/workflows/{workflow['id']}/schedules", json=schedule_payload(), headers=account["headers"]).json()
    assert client.get(f"/api/v1/schedules/{schedule['id']}", headers=other_account["headers"]).status_code == 404
    assert client.delete(f"/api/v1/schedules/{schedule['id']}", headers=other_account["headers"]).status_code == 404
    assert client.get("/api/v1/schedules", headers=other_account["headers"]).json()["total"] == 0
    assert client.post(f"/api/v1/workflows/{workflow['id']}/schedules", json=schedule_payload(), headers=other_account["headers"]).status_code == 404


def test_run_now_fires_a_schedule_immediately(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    schedule = client.post(f"/api/v1/workflows/{workflow['id']}/schedules", json=schedule_payload(), headers=account["headers"]).json()
    fired = client.post(f"/api/v1/schedules/{schedule['id']}/run-now", headers=account["headers"])
    assert fired.status_code == 200
    body = fired.json()
    assert body["status"] == "started"
    assert body["run_id"]

    run = client.get(f"/api/v1/runs/{body['run_id']}", headers=account["headers"]).json()
    assert run["trigger"] == "schedule"
    assert run["input"] == {"env": "prod"}
    assert run["version"] == workflow["version"]

    refreshed = client.get(f"/api/v1/schedules/{schedule['id']}", headers=account["headers"]).json()
    assert refreshed["run_count"] == 1
    assert refreshed["last_run_id"] == body["run_id"]


def test_scheduler_tick_is_idempotent_for_one_slot(client, account, workflow_factory, db_session):
    """Two ticks for the same occurrence must not create two runs."""
    from app.services import scheduler_service

    workflow = workflow_factory(account["headers"])
    schedule = client.post(f"/api/v1/workflows/{workflow['id']}/schedules", json=schedule_payload(), headers=account["headers"]).json()

    # Make the schedule due now.
    from app.models.schedule import WorkflowSchedule

    row = db_session.get(WorkflowSchedule, schedule["id"])
    row.next_run_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    db_session.commit()

    first = scheduler_service.tick(db_session)
    db_session.commit()
    assert first == 1

    # Force it due again for the same slot: duplicate protection must hold.
    row = db_session.get(WorkflowSchedule, schedule["id"])
    row.next_run_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    db_session.commit()
    second = scheduler_service.tick(db_session)
    db_session.commit()
    assert second == 0

    runs = client.get("/api/v1/runs", params={"workflow_id": workflow["id"]}, headers=account["headers"]).json()
    assert runs["total"] == 1
    assert runs["items"][0]["trigger"] == "schedule"


def test_idempotency_key_blocks_a_duplicate_scheduled_run(client, account, workflow_factory, db_session):
    """Even if the scheduler is forced to re-fire a slot, the unique index holds."""
    from app.services import run_service, scheduler_service, workflow_service

    workflow = workflow_factory(account["headers"])
    # overlap_policy=allow isolates the idempotency guard from overlap handling.
    schedule = client.post(
        f"/api/v1/workflows/{workflow['id']}/schedules",
        json=schedule_payload(overlap_policy="allow"),
        headers=account["headers"],
    ).json()

    from app.models.schedule import WorkflowSchedule

    row = db_session.get(WorkflowSchedule, schedule["id"])
    occurrence = datetime.now(timezone.utc).replace(tzinfo=None)
    first = scheduler_service.fire_schedule(db_session, row, slot=occurrence.replace(tzinfo=timezone.utc))
    db_session.commit()
    assert first["status"] == "started"

    # Simulate a replica that lost track of last_fired_slot and tries again.
    row = db_session.get(WorkflowSchedule, schedule["id"])
    row.last_fired_slot = None
    db_session.commit()
    again = scheduler_service.fire_schedule(db_session, row, slot=occurrence.replace(tzinfo=timezone.utc))
    db_session.commit()
    assert again["status"] == "started"
    # Same run, not a second one: the idempotency key was reused.
    assert again["run_id"] == first["run_id"]

    runs = client.get("/api/v1/runs", params={"workflow_id": workflow["id"]}, headers=account["headers"]).json()
    assert runs["total"] == 1


def test_overlap_policy_skip_prevents_a_second_active_run(client, account, workflow_factory, db_session):
    from app.models.schedule import WorkflowSchedule
    from app.services import scheduler_service

    workflow = workflow_factory(account["headers"])
    schedule = client.post(
        f"/api/v1/workflows/{workflow['id']}/schedules",
        json=schedule_payload(overlap_policy="skip"),
        headers=account["headers"],
    ).json()

    row = db_session.get(WorkflowSchedule, schedule["id"])
    first = scheduler_service.fire_schedule(db_session, row, slot=datetime.now(timezone.utc))
    db_session.commit()
    assert first["status"] == "started"

    # The first run is still queued; a second occurrence must be skipped.
    row = db_session.get(WorkflowSchedule, schedule["id"])
    second = scheduler_service.fire_schedule(db_session, row, slot=datetime.now(timezone.utc) + timedelta(hours=1))
    db_session.commit()
    assert second["status"] == "skipped"
    assert "still active" in second["message"]

    runs = client.get("/api/v1/runs", params={"workflow_id": workflow["id"]}, headers=account["headers"]).json()
    assert runs["total"] == 1


def test_overlap_policy_allow_starts_another_run(client, account, workflow_factory, db_session):
    from app.models.schedule import WorkflowSchedule
    from app.services import scheduler_service

    workflow = workflow_factory(account["headers"])
    schedule = client.post(
        f"/api/v1/workflows/{workflow['id']}/schedules",
        json=schedule_payload(overlap_policy="allow"),
        headers=account["headers"],
    ).json()

    row = db_session.get(WorkflowSchedule, schedule["id"])
    scheduler_service.fire_schedule(db_session, row, slot=datetime.now(timezone.utc))
    db_session.commit()
    row = db_session.get(WorkflowSchedule, schedule["id"])
    second = scheduler_service.fire_schedule(db_session, row, slot=datetime.now(timezone.utc) + timedelta(hours=1))
    db_session.commit()
    assert second["status"] == "started"

    runs = client.get("/api/v1/runs", params={"workflow_id": workflow["id"]}, headers=account["headers"]).json()
    assert runs["total"] == 2


def test_timezone_aware_next_run_uses_the_schedule_zone(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    schedule = client.post(
        f"/api/v1/workflows/{workflow['id']}/schedules",
        json=schedule_payload(cron_expression="0 9 * * *", timezone="Asia/Tokyo"),
        headers=account["headers"],
    ).json()
    # 09:00 JST == 00:00 UTC.
    next_run = datetime.fromisoformat(schedule["next_run_at"].replace("Z", "+00:00"))
    assert next_run.astimezone(timezone.utc).hour == 0
    assert schedule["upcoming"]


def test_schedule_activity_is_recorded(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    schedule = client.post(f"/api/v1/workflows/{workflow['id']}/schedules", json=schedule_payload(), headers=account["headers"]).json()
    client.post(f"/api/v1/schedules/{schedule['id']}/run-now", headers=account["headers"])
    activity = client.get(f"/api/v1/workflows/{workflow['id']}/activity", headers=account["headers"]).json()
    types = {item["type"] for item in activity}
    assert "schedule.created" in types
    assert "schedule.triggered" in types


def test_ops_scheduler_tick_endpoint(client, account, workflow_factory, db_session):
    from app.models.schedule import WorkflowSchedule

    workflow = workflow_factory(account["headers"])
    schedule = client.post(f"/api/v1/workflows/{workflow['id']}/schedules", json=schedule_payload(), headers=account["headers"]).json()
    row = db_session.get(WorkflowSchedule, schedule["id"])
    row.next_run_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    db_session.commit()

    response = client.post("/api/v1/ops/maintenance/scheduler-tick", headers=account["headers"])
    assert response.status_code == 200
    assert response.json()["runs_started"] == 1


def test_schedule_interval_is_available_to_worker_input_without_changing_run_input(client, account, workflow_factory, db_session):
    workflow = workflow_factory(
        account["headers"],
        {"name": "Interval template", "steps": [{"id": "echo", "type": "demo.echo", "input": {"value": "{{input.logical_date}}"}, "depends_on": []}]},
    )
    schedule = client.post(
        f"/api/v1/workflows/{workflow['id']}/schedules",
        json=schedule_payload(data_interval_seconds=3600),
        headers=account["headers"],
    ).json()
    fired = client.post(f"/api/v1/schedules/{schedule['id']}/run-now", headers=account["headers"]).json()
    detail = client.get(f"/api/v1/runs/{fired['run_id']}", headers=account["headers"]).json()
    assert detail["input"] == {"env": "prod"}
    assert detail["logical_date"] is not None
    assert detail["interval_end"] == detail["logical_date"]
    assert detail["interval_start"] < detail["interval_end"]
    assert datetime.fromisoformat(detail["interval_end"].replace("Z", "+00:00")) - datetime.fromisoformat(detail["interval_start"].replace("Z", "+00:00")) == timedelta(hours=1)

    from app.models.run import WorkflowRun
    from app.services.run_service import workflow_input_for_run

    run = db_session.get(WorkflowRun, fired["run_id"])
    worker_input = workflow_input_for_run(run)
    assert worker_input["logical_date"] == detail["logical_date"]
    assert worker_input["data_interval"]["interval_start"] == detail["interval_start"]


def test_backfill_pumps_slots_with_bounded_concurrency_and_completes(client, account, workflow_factory, db_session):
    from app.models.run import WorkflowRun
    from app.models.schedule import ScheduleBackfill
    from app.services import scheduler_service
    from sqlalchemy import select

    workflow = workflow_factory(account["headers"])
    schedule = client.post(
        f"/api/v1/workflows/{workflow['id']}/schedules",
        json=schedule_payload(cron_expression="*/15 * * * *", data_interval_seconds=900, jitter_seconds=1200),
        headers=account["headers"],
    ).json()
    response = client.post(
        f"/api/v1/schedules/{schedule['id']}/backfill",
        json={"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:46:00Z", "concurrency_limit": 2},
        headers=account["headers"],
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    runs = list(db_session.scalars(select(WorkflowRun).where(WorkflowRun.backfill_id == job_id)).all())
    assert len(runs) == 2
    assert all(run.input_data == {"env": "prod"} for run in runs)
    assert all(run.interval_end == run.logical_date for run in runs)
    assert all(run.logical_date - run.interval_start == timedelta(minutes=15) for run in runs)

    runs[0].status = "succeeded"
    db_session.commit()
    assert scheduler_service.pump_backfills(db_session) == 1
    db_session.commit()
    runs = list(db_session.scalars(select(WorkflowRun).where(WorkflowRun.backfill_id == job_id)).all())
    assert len(runs) == 3
    assert sum(run.status not in {"succeeded", "failed", "cancelled"} for run in runs) == 2

    for run in runs:
        run.status = "succeeded"
    db_session.commit()
    assert scheduler_service.pump_backfills(db_session) == 1
    db_session.commit()
    last = db_session.scalar(select(WorkflowRun).where(WorkflowRun.backfill_id == job_id, WorkflowRun.logical_date >= datetime(2026, 1, 1, 0, 45)))
    assert last is not None
    last.status = "succeeded"
    db_session.commit()
    scheduler_service.pump_backfills(db_session)
    db_session.commit()
    job = db_session.get(ScheduleBackfill, job_id)
    assert job.status == "completed"
    listed = client.get(f"/api/v1/schedules/{schedule['id']}/backfills", headers=account["headers"]).json()
    assert listed["items"][0]["status"] == "completed"


def test_jitter_preserves_nominal_logical_date_and_cron_interval(client, account, workflow_factory):
    workflow = workflow_factory(account["headers"])
    schedule = client.post(
        f"/api/v1/workflows/{workflow['id']}/schedules",
        json=schedule_payload(cron_expression="*/15 * * * *", jitter_seconds=900),
        headers=account["headers"],
    ).json()
    fired = client.post(f"/api/v1/schedules/{schedule['id']}/run-now", headers=account["headers"]).json()
    detail = client.get(f"/api/v1/runs/{fired['run_id']}", headers=account["headers"]).json()
    logical = datetime.fromisoformat(detail["logical_date"].replace("Z", "+00:00"))
    start = datetime.fromisoformat(detail["interval_start"].replace("Z", "+00:00"))
    assert logical.minute % 15 == 0 and logical.second == 0
    assert logical - start == timedelta(minutes=15)


def test_calendar_skips_weekends_holidays_and_pause_windows():
    from types import SimpleNamespace

    from app.services.scheduler_service import compute_next_run

    schedule = SimpleNamespace(
        id="calendar-test",
        cron_expression="0 9 * * *",
        timezone="UTC",
        skip_weekends=True,
        skip_dates=["2026-10-05"],
        pause_windows=[{"start": "2026-10-06T00:00:00", "end": "2026-10-06T23:59:59"}],
        jitter_seconds=0,
    )
    next_run = compute_next_run(schedule, after=datetime(2026, 10, 2, 9, tzinfo=timezone.utc))
    assert next_run == datetime(2026, 10, 7, 9)
