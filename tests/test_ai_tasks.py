from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

from app.worker import ai_tasks
from app.worker.handlers import registry


class Context:
    attempt = 2
    api_client = None

    def __init__(self):
        self.task = {"dependency_outputs": {}, "workflow_input": {}}
        self.messages = []

    def log(self, message, **fields):
        self.messages.append((message, fields))

    def cancelled(self):
        return False


class FakeModels:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.request = None

    def create(self, **request):
        self.request = request
        if self.error:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, models):
        self.interactions = models


def text_pdf(text: str) -> bytes:
    content = f"BT /F1 12 Tf 24 100 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(pdf)


def test_ai_and_document_handlers_are_registered():
    assert {"ai.summarize", "ai.classify", "document.extract_text"} <= set(registry.types())


@pytest.mark.parametrize("payload", [{}, {"text": ""}, {"text": "  "}])
def test_ai_summarize_rejects_missing_or_empty_text(payload):
    with pytest.raises(ai_tasks.TaskInputError):
        ai_tasks.summarize_text(payload, Context())


def test_ai_summarize_uses_gemini_and_returns_structured_result(monkeypatch):
    models = FakeModels(response=type("Response", (), {"output_text": "A concise summary."})())
    monkeypatch.setattr(ai_tasks.settings, "ai_provider", "gemini")
    monkeypatch.setattr(ai_tasks.settings, "gemini_model", "gemini-test-flash")
    monkeypatch.setattr(ai_tasks, "_gemini_client", lambda: FakeClient(models))

    result = ai_tasks.summarize_text({"text": "Some source text."}, Context())

    assert result == {
        "summary": "A concise summary.",
        "input_characters": len("Some source text."),
        "output_characters": len("A concise summary."),
        "model": "gemini-test-flash",
        "ai_usage": {"model": "gemini-test-flash"},
        "attempt": 2,
    }
    assert models.request["model"] == "gemini-test-flash"
    assert "Some source text." in models.request["input"]
    assert models.request["store"] is False


def test_ai_summarize_fails_clearly_without_api_key(monkeypatch):
    monkeypatch.setattr(ai_tasks.settings, "ai_provider", "gemini")
    monkeypatch.setattr(ai_tasks.settings, "gemini_api_key", "")

    with pytest.raises(ai_tasks.AIConfigurationError, match="GEMINI_API_KEY") as error:
        ai_tasks.summarize_text({"text": "Source text."}, Context())

    assert error.value.retryable is False


def test_ai_provider_failure_is_retryable_and_does_not_fabricate_success(monkeypatch):
    models = FakeModels(error=TimeoutError("provider unavailable"))
    monkeypatch.setattr(ai_tasks.settings, "ai_provider", "gemini")
    monkeypatch.setattr(ai_tasks, "_gemini_client", lambda: FakeClient(models))

    with pytest.raises(ai_tasks.AIProviderRequestError, match="Gemini request failed") as error:
        ai_tasks.summarize_text({"text": "Source text."}, Context())

    assert error.value.retryable is True


def test_ai_summarize_uses_groq_chat_completions(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "A concise summary."}}]}

    request = {}

    def fake_post(url, **kwargs):
        request.update(url=url, **kwargs)
        return FakeResponse()

    monkeypatch.setattr(ai_tasks.settings, "ai_provider", "groq")
    monkeypatch.setattr(ai_tasks.settings, "groq_api_key", "test-groq-key")
    monkeypatch.setattr(ai_tasks.settings, "groq_model", "test-model")
    monkeypatch.setattr(ai_tasks.httpx, "post", fake_post)

    result = ai_tasks.summarize_text({"text": "Some source text."}, Context())

    assert result["summary"] == "A concise summary."
    assert result["model"] == "test-model"
    assert request["url"] == ai_tasks.GROQ_CHAT_COMPLETIONS_URL
    assert request["headers"] == {"Authorization": "Bearer test-groq-key"}
    assert request["json"]["model"] == "test-model"
    assert request["json"]["messages"][0]["role"] == "system"
    assert "untrusted data" in request["json"]["messages"][0]["content"]
    assert "Some source text." in request["json"]["messages"][1]["content"]


def test_ai_classify_uses_groq_json_mode(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": '{"category":"invoice","reason":"Shows a total."}'}}]}

    request = {}

    def fake_post(_url, **kwargs):
        request.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(ai_tasks.settings, "ai_provider", "groq")
    monkeypatch.setattr(ai_tasks.settings, "groq_api_key", "test-groq-key")
    monkeypatch.setattr(ai_tasks.httpx, "post", fake_post)

    result = ai_tasks.classify_text({"text": "Invoice total: $25"}, Context())

    assert result["category"] == "invoice"
    assert result["reason"] == "Shows a total."
    assert request["json"]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(("status_code", "retryable"), [(401, False), (429, True), (503, True)])
def test_groq_http_errors_have_correct_retry_policy(monkeypatch, status_code, retryable):
    request = httpx.Request("POST", ai_tasks.GROQ_CHAT_COMPLETIONS_URL)
    response = httpx.Response(status_code, request=request)

    class FakeResponse:
        def raise_for_status(self):
            raise httpx.HTTPStatusError("provider response", request=request, response=response)

    monkeypatch.setattr(ai_tasks.settings, "ai_provider", "groq")
    monkeypatch.setattr(ai_tasks.settings, "groq_api_key", "test-groq-key")
    monkeypatch.setattr(ai_tasks.httpx, "post", lambda *_args, **_kwargs: FakeResponse())

    with pytest.raises(ai_tasks.AIProviderRequestError, match=f"HTTP {status_code}") as error:
        ai_tasks.summarize_text({"text": "Source text."}, Context())

    assert error.value.retryable is retryable
    assert "test-groq-key" not in str(error.value)


def test_groq_reports_missing_api_key_without_retrying(monkeypatch):
    monkeypatch.setattr(ai_tasks.settings, "ai_provider", "groq")
    monkeypatch.setattr(ai_tasks.settings, "groq_api_key", "")

    with pytest.raises(ai_tasks.AIConfigurationError, match="GROQ_API_KEY") as error:
        ai_tasks.summarize_text({"text": "Source text."}, Context())

    assert error.value.retryable is False


def test_ai_classify_parses_structured_json_and_dependency_text(monkeypatch):
    models = FakeModels(
        response=type(
            "Response",
            (),
            {"output_text": json.dumps({"category": "invoice", "reason": "Contains a total."})},
        )()
    )
    monkeypatch.setattr(ai_tasks.settings, "ai_provider", "gemini")
    monkeypatch.setattr(ai_tasks, "_gemini_client", lambda: FakeClient(models))
    context = Context()
    context.task["dependency_outputs"] = {"extract": {"text": "Invoice total: $25"}}

    result = ai_tasks.classify_text({"source_step": "extract"}, context)

    assert result["category"] == "invoice"
    assert result["reason"] == "Contains a total."
    assert "Invoice total" in models.request["input"]
    assert models.request["response_format"]["mime_type"] == "application/json"
    assert models.request["response_format"]["schema"]["required"] == ["category", "reason"]


def test_document_extract_reads_bounded_pdf_from_workflow_input(monkeypatch):
    class FakePage:
        def extract_text(self):
            return "Extracted document text"

    class FakeReader:
        is_encrypted = False
        pages = [FakePage(), FakePage()]

        def __init__(self, _stream, strict=False):
            assert strict is False

    monkeypatch.setattr("pypdf.PdfReader", FakeReader)
    context = Context()
    context.task["workflow_input"] = {"uploaded_pdf": base64.b64encode(b"%PDF-1.7 test").decode()}

    result = ai_tasks.extract_pdf_text({"source_key": "uploaded_pdf"}, context)

    assert result == {
        "text": "Extracted document text\nExtracted document text",
        "page_count": 2,
        "characters": len("Extracted document text\nExtracted document text"),
        "truncated": False,
    }
    assert context.messages[0][1] == {
        "page_count": 2,
        "characters": result["characters"],
        "truncated": False,
    }


def test_document_extract_reads_real_pdf_text():
    context = Context()
    context.task["workflow_input"] = {
        "document_pdf_base64": base64.b64encode(text_pdf("Invoice total due: $450.")).decode()
    }

    result = ai_tasks.extract_pdf_text({}, context)

    assert result["page_count"] == 1
    assert "Invoice total due: $450." in result["text"]
    assert result["truncated"] is False


def test_document_extract_rejects_bad_pdf_input():
    context = Context()
    context.task["workflow_input"] = {"document_pdf_base64": base64.b64encode(b"not a pdf").decode()}

    with pytest.raises(ai_tasks.TaskInputError, match="not a PDF"):
        ai_tasks.extract_pdf_text({}, context)


def test_document_workflow_runs_through_worker_and_preserves_step_outputs(
    client, account, worker_client, workflow_factory, monkeypatch
):
    from app.worker.registry import registry as worker_registry
    from app.worker.runtime import Worker

    class FakePage:
        def extract_text(self):
            return "An invoice for consulting services, total due $450."

    class FakeReader:
        is_encrypted = False
        pages = [FakePage()]

        def __init__(self, _stream, strict=False):
            pass

    monkeypatch.setattr("pypdf.PdfReader", FakeReader)

    def generate(prompt, _ctx, *, json_response=False):
        if json_response:
            return json.dumps({"category": "invoice", "reason": "The text lists an amount due."}), None
        return "An invoice for consulting services with $450 due.", None

    monkeypatch.setattr(ai_tasks, "_generate", generate)
    workflow = workflow_factory(
        account["headers"],
        {
            "name": "AI document processing",
            "description": "Extract, summarize and classify a PDF.",
            "default_max_parallel": 2,
            "steps": [
                {
                    "id": "extract",
                    "type": "document.extract_text",
                    "input": {"source_key": "document_pdf_base64"},
                    "depends_on": [],
                    "retries": 0,
                    "timeout_seconds": 120,
                },
                {
                    "id": "summarize",
                    "type": "ai.summarize",
                    "input": {"source_step": "extract"},
                    "depends_on": ["extract"],
                    "retries": 2,
                    "timeout_seconds": 300,
                },
                {
                    "id": "classify",
                    "type": "ai.classify",
                    "input": {"source_step": "extract"},
                    "depends_on": ["extract", "summarize"],
                    "retries": 2,
                    "timeout_seconds": 300,
                },
            ],
        },
    )
    started = client.post(
        f"/api/v1/workflows/{workflow['id']}/runs",
        json={"input": {"document_pdf_base64": base64.b64encode(b"%PDF-1.7 test").decode()}},
        headers=account["headers"],
    )
    assert started.status_code == 201, started.text

    class TestApi:
        timeout = 30.0

        def post(self, path, body=None, *, timeout=None):
            response = worker_client["client"].post(path, json=body)
            assert response.status_code < 400, response.text
            return response.json()

    worker = Worker(
        token=worker_client["token"],
        worker_id=worker_client["worker_id"],
        registry=worker_registry,
        poll_seconds=0.01,
    )
    worker.api = TestApi()
    run_id = started.json()["id"]
    try:
        worker.register()
        deadline = time.monotonic() + 10
        detail = None
        while time.monotonic() < deadline:
            worker._claim_once()
            while worker._inflight_count() and time.monotonic() < deadline:
                time.sleep(0.01)
            response = client.get(f"/api/v1/runs/{run_id}", headers=account["headers"])
            assert response.status_code == 200, response.text
            detail = response.json()
            if detail["status"] in {"succeeded", "failed", "cancelled"}:
                break

        assert detail is not None
        assert detail["status"] == "succeeded"
        steps = {step["key"]: step for step in detail["steps"]}
        assert steps["extract"]["output"]["text"] == "An invoice for consulting services, total due $450."
        assert steps["summarize"]["output"]["summary"].startswith("An invoice")
        assert steps["classify"]["output"]["category"] == "invoice"
        assert all(steps[key]["status"] == "succeeded" for key in steps)
    finally:
        worker.shutdown()
