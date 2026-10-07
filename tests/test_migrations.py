"""Migration regression checks against isolated SQLite databases."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "d8e21a6c0f34"
REPAIR_REVISION = "b7f04c9a12de"


def _alembic_head() -> str:
    """Read the single head from the migration tree instead of hardcoding it."""
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    heads = [line.split()[0] for line in proc.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, f"expected exactly one alembic head, got: {proc.stdout}"
    return heads[0]


HEAD_REVISION = _alembic_head()


@pytest.mark.parametrize("schema_state", ["missing_column", "nullable_without_default"])
def test_required_column_migration_repairs_schema_and_preserves_rows(tmp_path: Path, schema_state: str) -> None:
    database_path = tmp_path / "migration-regression.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        if schema_state == "missing_column":
            connection.execute(
                "CREATE TABLE step_runs (id VARCHAR(32) PRIMARY KEY, step_key VARCHAR(128) NOT NULL)"
            )
            connection.execute(
                "INSERT INTO step_runs (id, step_key) VALUES (?, ?)", ("legacy-step", "extract")
            )
        else:
            connection.execute(
                "CREATE TABLE step_runs ("
                "id VARCHAR(32) PRIMARY KEY, step_key VARCHAR(128) NOT NULL, required BOOLEAN)"
            )
            connection.execute(
                "INSERT INTO step_runs (id, step_key, required) VALUES (?, ?, NULL)",
                ("legacy-step", "extract"),
            )
        connection.execute(
            "INSERT INTO alembic_version (version_num) VALUES (?)", (PREVIOUS_REVISION,)
        )
        connection.commit()
    finally:
        connection.close()

    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
    upgraded = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

    connection = sqlite3.connect(database_path)
    try:
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(step_runs)")}
        assert "required" in columns
        assert columns["required"][3] == 1  # NOT NULL
        assert str(columns["required"][4]).strip("()'").lower() in {"1", "true"}
        assert connection.execute(
            "SELECT id, step_key, required FROM step_runs WHERE id = ?", ("legacy-step",)
        ).fetchone() == ("legacy-step", "extract", 1)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
    finally:
        connection.close()


def test_phase1_migration_runs_twice_and_creates_execution_storage(tmp_path: Path) -> None:
    database_path = tmp_path / "phase1.sqlite3"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"

    for _ in range(2):
        upgraded = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

    connection = sqlite3.connect(database_path)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"step_cache_entries", "run_artifacts", "workflow_triggers", "trigger_deliveries", "schedule_backfills"}.issubset(tables)
        run_columns = {row[1] for row in connection.execute("PRAGMA table_info(workflow_runs)")}
        run_column_info = {row[1]: row for row in connection.execute("PRAGMA table_info(workflow_runs)")}
        step_columns = {row[1] for row in connection.execute("PRAGMA table_info(step_runs)")}
        schedule_columns = {row[1] for row in connection.execute("PRAGMA table_info(workflow_schedules)")}
        assert {"definition_json", "deadline_at", "sla_deadline_at", "parent_run_id", "nesting_depth", "priority", "queue_name"}.issubset(run_columns)
        assert run_column_info["triggered_by"][2].lower() == "varchar(200)"
        assert {"spec_json", "parent_step_id", "foreach_index", "child_run_id", "priority", "queue_name", "concurrency_key", "concurrency_limit", "lease_expirations"}.issubset(step_columns)
        assert {"concurrency_gates", "task_rate_buckets"}.issubset(tables)
        worker_columns = {row[1] for row in connection.execute("PRAGMA table_info(workers)")}
        assert "queues" in worker_columns
        assert {"data_interval_seconds", "jitter_seconds", "skip_weekends", "skip_dates", "pause_windows"}.issubset(schedule_columns)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD_REVISION,)
    finally:
        connection.close()
