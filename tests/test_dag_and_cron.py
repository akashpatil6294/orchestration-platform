"""Unit tests for the pure logic: DAG validation and cron arithmetic."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core import cron, dag


# --------------------------------------------------------------------------- #
# DAG validation
# --------------------------------------------------------------------------- #
def definition(*steps):
    return {"name": "test", "description": "", "steps": list(steps)}


def step(step_id, *, depends_on=None, **extra):
    payload = {"id": step_id, "type": "demo.echo", "input": {}, "depends_on": depends_on or []}
    payload.update(extra)
    return payload


def codes(issues):
    return {issue["code"] for issue in issues}


def test_valid_definition_has_no_issues():
    issues = dag.validate_definition(definition(step("a"), step("b", depends_on=["a"]), step("c", depends_on=["a"])))
    assert issues == []


def test_requires_at_least_one_step():
    assert "steps.required" in codes(dag.validate_definition({"name": "x", "steps": []}))


def test_rejects_duplicate_step_ids():
    issues = dag.validate_definition(definition(step("a"), step("a")))
    assert "step.id_duplicate" in codes(issues)


def test_rejects_unknown_dependency():
    issues = dag.validate_definition(definition(step("a", depends_on=["ghost"])))
    assert "step.depends_on_unknown" in codes(issues)


def test_rejects_step_output_reference_without_declared_dependency():
    issues = dag.validate_definition(
        definition(step("source"), step("consumer", input={"value": "{{steps.source.output.value}}"}))
    )
    assert "step.input_reference_invalid" in codes(issues)


def test_accepts_workflow_input_and_declared_dependency_references():
    issues = dag.validate_definition(
        definition(
            step("source"),
            step(
                "consumer",
                depends_on=["source"],
                input={
                    "value": "{{steps.source.output.value}}",
                    "tenant": "{{input.tenant.id}}",
                },
            ),
        )
    )
    assert issues == []


def test_rejects_self_dependency():
    issues = dag.validate_definition(definition(step("a", depends_on=["a"])))
    assert "step.depends_on_self" in codes(issues)


def test_detects_cycles():
    issues = dag.validate_definition(
        definition(step("a", depends_on=["c"]), step("b", depends_on=["a"]), step("c", depends_on=["b"]))
    )
    assert "graph.cycle" in codes(issues)


def test_rejects_reserved_task_prefix():
    issues = dag.validate_definition(definition({"id": "a", "type": "orchestrator.internal", "input": {}}))
    assert "step.type_reserved" in codes(issues)


def test_rejects_unknown_step_fields():
    issues = dag.validate_definition(definition(step("a", shell="rm -rf /")))
    assert "step.unknown_field" in codes(issues)


def test_rejects_bad_retry_and_timeout_values():
    issues = dag.validate_definition(definition(step("a", retries=99, timeout_seconds=0)))
    assert {"step.retries_invalid", "step.timeout_invalid"} <= codes(issues)


def test_rejects_non_boolean_required_policy():
    issues = dag.validate_definition(definition(step("a", required="no")))
    assert "step.required_invalid" in codes(issues)


def test_rejects_invalid_step_id_characters():
    issues = dag.validate_definition(definition({"id": "has space", "type": "demo.echo", "input": {}}))
    assert "step.id_invalid" in codes(issues)


def test_enforces_step_limit():
    many = definition(*[step(f"s{i}") for i in range(11)])
    issues = dag.validate_definition(many, max_steps=10)
    assert "steps.too_many" in codes(issues)


def test_rejects_oversized_step_input():
    huge = definition(step("a", input={"blob": "x" * 70_000}))
    assert "step.input_too_large" in codes(dag.validate_definition(huge))


def test_topological_order_is_deterministic_and_respects_dependencies():
    edges = {"c": ["a", "b"], "a": [], "b": [], "d": ["c"]}
    order = dag.topological_order(edges)
    assert order.index("a") < order.index("c")
    assert order.index("b") < order.index("c")
    assert order.index("c") < order.index("d")


def test_topological_order_raises_on_cycle():
    with pytest.raises(ValueError):
        dag.topological_order({"a": ["b"], "b": ["a"]})


def test_levels_group_parallel_steps():
    edges = {"a": [], "b": [], "c": ["a", "b"], "d": ["c"]}
    assert dag.levels(edges) == {"a": 0, "b": 0, "c": 1, "d": 2}


def test_downstream_of_finds_transitive_children():
    edges = {"a": [], "b": ["a"], "c": ["b"], "d": ["a"], "e": []}
    assert dag.downstream_of(edges, {"a"}) == {"b", "c", "d"}


def test_summarize_definition_reports_shape():
    summary = dag.summarize_definition(definition(step("a"), step("b", depends_on=["a"])))
    assert summary["step_count"] == 2
    assert summary["edge_count"] == 1
    assert summary["max_depth"] == 2
    assert summary["task_types"] == ["demo.echo"]


# --------------------------------------------------------------------------- #
# Cron
# --------------------------------------------------------------------------- #
def test_every_minute_matches_every_minute():
    schedule = cron.CronSchedule("* * * * *")
    moment = datetime(2026, 3, 1, 12, 34, tzinfo=timezone.utc)
    assert schedule.matches(moment)


def test_daily_macro_is_equivalent_to_expression():
    assert cron.normalize_expression("@daily") == "0 0 * * *"
    assert cron.CronSchedule("@daily").expression == "0 0 * * *"


def test_next_after_advances_to_the_next_matching_minute():
    schedule = cron.CronSchedule("*/15 * * * *")
    reference = datetime(2026, 3, 1, 12, 7, tzinfo=timezone.utc)
    nxt = schedule.next_after(reference)
    assert nxt == datetime(2026, 3, 1, 12, 15, tzinfo=timezone.utc)


def test_next_after_respects_timezone_wall_clock():
    # 09:00 in New York is 14:00 UTC during EDT.
    nxt = cron.next_run_at("0 9 * * *", "America/New_York", after=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc))
    assert nxt.astimezone(timezone.utc) == datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc)


def test_timezone_changes_are_reflected_across_dst():
    # Same wall-clock rule, different UTC instant either side of the DST switch.
    summer = cron.next_run_at("30 8 * * *", "Europe/Berlin", after=datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc))
    winter = cron.next_run_at("30 8 * * *", "Europe/Berlin", after=datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc))
    assert summer.hour == 6  # UTC+2
    assert winter.hour == 7  # UTC+1


def test_day_of_week_and_day_of_month_use_or_semantics():
    schedule = cron.CronSchedule("0 0 1 * 1")
    monday = datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)  # a Monday, not the 1st
    first = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)  # the 1st, a Wednesday
    assert schedule.matches(monday)
    assert schedule.matches(first)


def test_weekday_names_are_accepted():
    schedule = cron.CronSchedule("0 9 * * mon-fri")
    assert schedule.matches(datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc))
    assert not schedule.matches(datetime(2026, 3, 7, 9, 0, tzinfo=timezone.utc))


def test_invalid_expressions_are_rejected_with_a_message():
    for expression in ("", "not a cron", "* * * *", "60 * * * *", "* * * 13 *", "*/0 * * * *"):
        assert cron.validate_expression(expression) is not None, expression


def test_upcoming_runs_are_strictly_increasing():
    runs = cron.upcoming_runs("0 */6 * * *", "UTC", 4, after=datetime(2026, 3, 1, 0, 30, tzinfo=timezone.utc))
    assert len(runs) == 4
    assert runs == sorted(runs)
    assert runs[0] == datetime(2026, 3, 1, 6, 0, tzinfo=timezone.utc)


def test_unknown_timezone_is_rejected():
    with pytest.raises(cron.CronError):
        cron.resolve_timezone("Mars/Olympus_Mons")


def test_slot_key_is_stable_per_minute():
    moment = datetime(2026, 3, 1, 12, 0, 45, tzinfo=timezone.utc)
    assert cron.slot_key(moment) == "2026-03-01T12:00Z"
    assert cron.slot_key(moment + timedelta(seconds=10)) == cron.slot_key(moment)


def test_previous_run_at_walks_backwards():
    previous = cron.previous_run_at("0 0 * * *", "UTC", before=datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc))
    assert previous == datetime(2026, 3, 5, 0, 0, tzinfo=timezone.utc)
