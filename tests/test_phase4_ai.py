"""Phase 4 AI task tests: ai.extract / ai.classify / ai.eval with mocked providers.

The provider boundary (``ai_tasks._generate``) is stubbed so no network or API
key is needed; the tests exercise schema validation, the auto-repair loop,
chunk merging, confidence routing, provider fallback policy and run-level
usage aggregation.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.worker import ai_tasks


class Context:
    attempt = 1

    def __init__(self):
        self.task = {"dependency_outputs": {}, "workflow_input": {}}
        self.messages = []

    def log(self, message, **fields):
        self.messages.append((message, fields))

    def cancelled(self):
        return False


SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": {"type": "string"},
        "total": {"type": "number"},
        "line_items": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["invoice_number", "total"],
}

VALID_EXTRACTION = {"invoice_number": "INV-2026-001", "total": 1234.56, "line_items": ["widgets"]}


def _use_test_model(monkeypatch):
    monkeypatch.setattr(ai_tasks.settings, "ai_provider", "groq")
    monkeypatch.setattr(ai_tasks.settings, "groq_model", "test-model")
    monkeypatch.setattr(ai_tasks.settings, "groq_api_key", "test-key")
    monkeypatch.setattr(
        ai_tasks.settings,
        "ai_model_prices",
        {"test-model": {"prompt": 1.0, "completion": 2.0}},
    )


def _stub_generate(monkeypatch, responses):
    """Serve canned (raw_json, usage) pairs from ai_tasks._generate in order."""
    calls = {"count": 0}

    def fake_generate(prompt, ctx, *, json_response=False):
        index = min(calls["count"], len(responses) - 1)
        calls["count"] += 1
        raw, usage = responses[index]
        return raw, usage

    monkeypatch.setattr(ai_tasks, "_generate", fake_generate)
    return calls


def test_extract_happy_path_normalises_usage_and_cost(monkeypatch):
    _use_test_model(monkeypatch)
    _stub_generate(
        monkeypatch,
        [(json.dumps(VALID_EXTRACTION), {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})],
    )

    result = ai_tasks.extract_structured({"text": "Invoice INV-2026-001 total 1234.56", "json_schema": SCHEMA}, Context())

    assert result["extracted"] == VALID_EXTRACTION
    assert result["chunks"] == 1
    assert result["repairs_used"] == 0
    assert result["model"] == "test-model"
    # 100 prompt tokens at $1.00/M + 50 completion tokens at $2.00/M.
    assert result["ai_usage"] == {
        "model": "test-model",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cost_usd": 0.0002,
    }


def test_extract_auto_repair_succeeds_on_second_attempt(monkeypatch):
    _use_test_model(monkeypatch)
    calls = _stub_generate(
        monkeypatch,
        [
            (json.dumps({"invoice_number": "INV-1"}), {"prompt_tokens": 10, "completion_tokens": 5}),
            (json.dumps({"invoice_number": "INV-1", "total": 42.0}), {"prompt_tokens": 10, "completion_tokens": 5}),
        ],
    )

    result = ai_tasks.extract_structured({"text": "some invoice text", "json_schema": SCHEMA}, Context())

    assert calls["count"] == 2
    assert result["repairs_used"] == 1
    assert result["extracted"]["total"] == 42.0
    # Usage accumulates across the first attempt and the repair.
    assert result["ai_usage"]["prompt_tokens"] == 20
    assert result["ai_usage"]["completion_tokens"] == 10


def test_extract_permanent_schema_failure_is_not_retryable(monkeypatch):
    _use_test_model(monkeypatch)
    _stub_generate(
        monkeypatch,
        [(json.dumps({"invoice_number": "INV-1"}), {"prompt_tokens": 10, "completion_tokens": 5})] * 3,
    )

    with pytest.raises(ai_tasks.AIProviderRequestError, match="did not match the schema") as error:
        ai_tasks.extract_structured({"text": "some invoice text", "json_schema": SCHEMA}, Context())

    assert error.value.retryable is False


def test_extract_chunks_long_text_and_merges_list_fields(monkeypatch):
    _use_test_model(monkeypatch)
    monkeypatch.setattr(ai_tasks.settings, "ai_chunk_chars", 500)
    monkeypatch.setattr(ai_tasks.settings, "ai_chunk_overlap_chars", 50)
    calls = _stub_generate(
        monkeypatch,
        [(json.dumps({"invoice_number": "INV-9", "total": 9.0, "line_items": ["item"]}), {"prompt_tokens": 5, "completion_tokens": 5})] * 64,
    )

    result = ai_tasks.extract_structured({"text": "x" * 1600, "json_schema": SCHEMA}, Context())

    assert result["chunks"] > 1
    assert calls["count"] == result["chunks"]
    # List fields from every chunk are concatenated.
    assert result["extracted"]["line_items"] == ["item"] * result["chunks"]
    assert result["ai_usage"]["prompt_tokens"] == 5 * result["chunks"]


def test_classify_low_confidence_fail_raises_without_retry(monkeypatch):
    _use_test_model(monkeypatch)
    _stub_generate(
        monkeypatch,
        [(json.dumps({"category": "invoice", "reason": "looks like one", "confidence": 0.1}), None)],
    )

    with pytest.raises(ai_tasks.AIProviderRequestError, match="below the configured threshold") as error:
        ai_tasks.classify_text(
            {"text": "some text", "min_confidence": 0.5, "on_low_confidence": "fail"},
            Context(),
        )

    assert error.value.retryable is False


def test_classify_low_confidence_flag_returns_flagged_result(monkeypatch):
    _use_test_model(monkeypatch)
    _stub_generate(
        monkeypatch,
        [(json.dumps({"category": "invoice", "reason": "looks like one", "confidence": 0.2}), None)],
    )

    result = ai_tasks.classify_text(
        {"text": "some text", "min_confidence": 0.5, "on_low_confidence": "flag"},
        Context(),
    )

    assert result["category"] == "invoice"
    assert result["confidence"] == 0.2
    assert result["low_confidence"] is True


def test_classify_missing_confidence_is_not_low_confidence(monkeypatch):
    _use_test_model(monkeypatch)
    _stub_generate(
        monkeypatch,
        [(json.dumps({"category": "invoice", "reason": "no score given"}), None)],
    )

    result = ai_tasks.classify_text({"text": "some text", "min_confidence": 0.5}, Context())

    assert result["confidence"] is None
    assert result["low_confidence"] is False


def test_provider_fallback_enabled_recovers_from_primary_failure(monkeypatch):
    monkeypatch.setattr(ai_tasks.settings, "ai_provider", "groq")
    monkeypatch.setattr(ai_tasks.settings, "ai_fallback_enabled", True)
    monkeypatch.setattr(ai_tasks.settings, "ai_fallback_provider", "gemini")
    monkeypatch.setattr(ai_tasks.settings, "gemini_model", "fallback-model")

    def boom(prompt, *, json_response=False):
        raise ai_tasks.AIProviderRequestError("Groq request failed.", retryable=True)

    monkeypatch.setattr(ai_tasks, "_primary_call", boom)
    monkeypatch.setattr(ai_tasks, "_fallback_call", lambda prompt, *, json_response=False: ("Fallback summary.", None))

    result = ai_tasks.summarize_text({"text": "Source text."}, Context())

    assert result["summary"] == "Fallback summary."
    # The model label still reflects the configured primary provider; the
    # important part is the fallback supplied the result.
    assert result["model"] == ai_tasks.settings.groq_model


def test_provider_fallback_disabled_surfaces_primary_failure(monkeypatch):
    monkeypatch.setattr(ai_tasks.settings, "ai_provider", "groq")
    monkeypatch.setattr(ai_tasks.settings, "ai_fallback_enabled", False)
    monkeypatch.setattr(ai_tasks.settings, "ai_fallback_provider", "gemini")

    def boom(prompt, *, json_response=False):
        raise ai_tasks.AIProviderRequestError("Groq request failed.", retryable=True)

    monkeypatch.setattr(ai_tasks, "_primary_call", boom)
    fallback_calls = {"count": 0}

    def fallback(prompt, *, json_response=False):
        fallback_calls["count"] += 1
        return ("should not happen", None)

    monkeypatch.setattr(ai_tasks, "_fallback_call", fallback)

    with pytest.raises(ai_tasks.AIProviderRequestError, match="Groq request failed"):
        ai_tasks.summarize_text({"text": "Source text."}, Context())
    assert fallback_calls["count"] == 0


def test_provider_fallback_is_off_by_default():
    assert ai_tasks.settings.ai_fallback_enabled is False
    assert ai_tasks.settings.ai_fallback_provider == ""


def test_eval_two_samples_reports_pass_rate_of_one_half(monkeypatch):
    _use_test_model(monkeypatch)
    # Both samples extract {"label": "yes"}; only the first expects it.
    _stub_generate(monkeypatch, [(json.dumps({"label": "yes"}), None)] * 2)

    result = ai_tasks.evaluate_extraction(
        {
            "samples": [
                {"input": "first", "expected": {"label": "yes"}},
                {"input": "second", "expected": {"label": "no"}},
            ],
        },
        Context(),
    )

    assert result["passed"] == 1
    assert result["total"] == 2
    assert result["pass_rate"] == 0.5
    assert [item["passed"] for item in result["results"]] == [True, False]


def test_run_stats_aggregate_ai_usage_across_steps(db_session):
    from app.core.security import hash_password
    from app.models.run import StepRun, WorkflowRun
    from app.models.user import User
    from app.models.workflow import Workflow
    from app.services import run_service

    email = f"ai-{uuid.uuid4().hex[:10]}@example.com"
    user = User(email=email, display_name="AI", password_hash=hash_password("testpassword123"), is_admin=False)
    db_session.add(user)
    db_session.flush()
    workflow = Workflow(owner_id=user.id, name="AI workflow", description="", draft={"name": "AI workflow", "steps": []}, default_max_parallel=2)
    db_session.add(workflow)
    db_session.flush()
    run = WorkflowRun(workflow_id=workflow.id, version=1, status="succeeded")
    db_session.add(run)
    db_session.flush()
    db_session.add(
        StepRun(
            run_id=run.id,
            step_key="extract",
            task_type="ai.extract",
            status="succeeded",
            output_data={
                "extracted": {"a": 1},
                "ai_usage": {
                    "model": "test-model",
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                    "cost_usd": 0.0002,
                },
            },
        )
    )
    db_session.add(
        StepRun(
            run_id=run.id,
            step_key="classify",
            task_type="ai.classify",
            status="succeeded",
            output_data={
                "category": "invoice",
                "ai_usage": {
                    "model": "test-model",
                    "prompt_tokens": 40,
                    "completion_tokens": 10,
                    "total_tokens": 50,
                    "cost_usd": 0.00006,
                },
            },
        )
    )
    db_session.add(
        StepRun(
            run_id=run.id,
            step_key="plain",
            task_type="demo.echo",
            status="succeeded",
            output_data={"value": "no ai here"},
        )
    )
    db_session.commit()

    stats = run_service.ai_usage_for_run(db_session, run.id)

    assert stats["ai_usage"]["steps"] == 2
    (model,) = stats["ai_usage"]["models"]
    assert model["model"] == "test-model"
    assert model["prompt_tokens"] == 140
    assert model["completion_tokens"] == 60
    assert model["total_tokens"] == 200
    assert model["cost_usd"] == pytest.approx(0.00026)
    assert model["steps"] == 2
