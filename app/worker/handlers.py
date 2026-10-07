"""Registered task handlers.

Demo handlers are dependency-free; AI and PDF tasks live in ``ai_tasks`` and
use the same explicit registry and worker context.

Patterns worth copying:

* **Idempotency.** ``demo.flaky_http`` keeps a process-local record of completed
  idempotency keys, so a redelivered task returns the original result instead of
  repeating a side effect.
* **Retries.** ``demo.fail_once`` fails deterministically on the first attempt and
  succeeds afterwards, which makes retry behaviour demonstrable and testable.
* **Cooperative cancellation.** Long handlers call ``ctx.cancelled()`` between
  units of work and stop early when a run is cancelled.
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from typing import Any

from app.worker.ai_tasks import classify_text, evaluate_extraction, extract_pdf_text, extract_structured, summarize_text
from app.worker.connector_tasks import email_send, http_request, slack_post, sql_query, storage_get, storage_put, transform_json, webhook_call
from app.worker.registry import TaskContext, registry

# Process-local memory of completed side effects, keyed by idempotency key.
_SIDE_EFFECT_LEDGER: dict[str, Any] = {}

@registry.register(
    "document.extract_text",
    description="Extract bounded text from an uploaded PDF in the workflow input",
    timeout_seconds=120,
    tags=("document",),
    category="document",
    icon_key="file-text",
    idempotent=True,
    input_schema={
        "type": "object",
        "properties": {
            "document_id": {"type": "string", "description": "ID of an uploaded document (preferred)"},
            "pdf_base64": {"type": "string", "description": "Inline base64 PDF (small files only)"},
            "document_pdf_base64": {"type": "string", "description": "Legacy inline base64 field"},
            "source_key": {"type": "string", "description": "Workflow-input key holding the PDF", "default": "document_pdf_base64"},
            "max_chars": {"type": "integer", "description": "Maximum characters to extract", "minimum": 1},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "characters": {"type": "integer"},
            "pages": {"type": "integer"},
        },
    },
    default_policy={"timeout_seconds": 120, "max_retries": 1, "backoff": "exponential"},
)
def extract_text(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    return extract_pdf_text(payload, ctx)


@registry.register(
    "ai.summarize",
    description="Generate a concise summary with the configured AI provider (Groq by default)",
    timeout_seconds=300,
    tags=("ai",),
    category="ai",
    icon_key="sparkles",
    idempotent=True,
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to summarize (or use source_step)"},
            "source_step": {"type": "string", "description": "Step ID whose output text to summarize"},
            "max_chars": {"type": "integer", "description": "Truncate input to this many characters", "minimum": 1},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "input_characters": {"type": "integer"},
            "output_characters": {"type": "integer"},
            "model": {"type": "string"},
        },
    },
    default_policy={"timeout_seconds": 300, "max_retries": 2, "backoff": "exponential"},
)
def ai_summarize(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    return summarize_text(payload, ctx)


@registry.register(
    "ai.classify",
    description="Classify text with the configured AI provider (Groq by default)",
    timeout_seconds=300,
    tags=("ai",),
    category="ai",
    icon_key="tag",
    idempotent=True,
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to classify (or use source_step)"},
            "source_step": {"type": "string", "description": "Step ID whose output text to classify"},
            "choices": {"type": "array", "items": {"type": "string"}, "description": "Allowed categories"},
            "categories": {"type": "array", "items": {"type": "string"}, "description": "Alias for choices"},
            "min_confidence": {"type": "number", "description": "Minimum accepted confidence (0-1)", "minimum": 0, "maximum": 1},
            "on_low_confidence": {"type": "string", "enum": ["fail", "flag"], "default": "flag", "description": "What to do below min_confidence"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "category": {"type": "string"},
            "confidence": {"type": "number"},
            "low_confidence": {"type": "boolean"},
            "model": {"type": "string"},
        },
        "required": ["category"],
    },
    default_policy={"timeout_seconds": 300, "max_retries": 2, "backoff": "exponential"},
)
def ai_classify(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    return classify_text(payload, ctx)


@registry.register(
    "ai.extract",
    description="Schema-validated extraction with one auto-repair retry (Groq by default)",
    timeout_seconds=300,
    tags=("ai",),
    category="ai",
    icon_key="braces",
    idempotent=True,
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to extract from (or use source_step)"},
            "source_step": {"type": "string", "description": "Step ID whose output text to extract from"},
            "prompt_template": {"type": "string", "description": "Extraction instructions with {{text}} placeholder"},
            "json_schema": {"type": "object", "description": "JSON Schema the extracted object must satisfy"},
        },
        "required": ["json_schema"],
    },
    output_schema={
        "type": "object",
        "description": "The extracted object, validated against the input json_schema",
    },
    default_policy={"timeout_seconds": 300, "max_retries": 2, "backoff": "exponential"},
)
def ai_extract(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    return extract_structured(payload, ctx)


@registry.register(
    "ai.eval",
    description="Score an extraction against expected samples and report the pass rate",
    timeout_seconds=600,
    tags=("ai", "eval"),
    category="ai",
    icon_key="check-circle",
    idempotent=True,
    input_schema={
        "type": "object",
        "properties": {
            "samples": {"type": "array", "description": "Expected extraction samples to score against", "items": {"type": "object"}},
            "source_step": {"type": "string", "description": "Step ID whose extraction output to evaluate"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "pass_rate": {"type": "number"},
            "passed": {"type": "integer"},
            "total": {"type": "integer"},
        },
        "required": ["pass_rate"],
    },
    default_policy={"timeout_seconds": 600, "max_retries": 1, "backoff": "exponential"},
)
def ai_eval(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    return evaluate_extraction(payload, ctx)


@registry.register(
    "http.request",
    description="SSRF-guarded outbound HTTP call (see connectors)",
    timeout_seconds=60,
    side_effects=True,
    tags=("connector",),
    category="connector",
    icon_key="globe",
    idempotent=False,
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri", "description": "Target URL (SSRF-guarded)"},
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"], "default": "GET"},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "params": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Query parameters"},
            "json": {"description": "JSON body"},
            "body": {"type": "string", "description": "Raw body"},
            "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 120},
        },
        "required": ["url"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "integer"},
            "body": {"type": "string"},
            "headers": {"type": "object"},
        },
    },
    default_policy={"timeout_seconds": 60, "max_retries": 3, "backoff": "exponential", "retry_on": "transient"},
)
def _http_entry(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    return http_request(payload, ctx)


@registry.register(
    "webhook.call",
    description="HMAC-signed outgoing webhook with retries",
    timeout_seconds=60,
    side_effects=True,
    tags=("connector",),
    category="connector",
    icon_key="webhook",
    idempotent=False,
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri", "description": "Webhook target URL"},
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH"], "default": "POST"},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "json": {"description": "JSON payload"},
            "body": {"type": "string", "description": "Raw payload"},
            "signing_secret": {"type": "string", "description": "Secret name for the HMAC signature"},
        },
        "required": ["url"],
    },
    output_schema={
        "type": "object",
        "properties": {"status": {"type": "integer"}, "body": {"type": "string"}},
    },
    default_policy={"timeout_seconds": 60, "max_retries": 5, "backoff": "exponential", "retry_on": "transient"},
)
def _webhook_entry(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    return webhook_call(payload, ctx)


@registry.register(
    "slack.post",
    description="Post to a Slack Incoming Webhook stored as a secret",
    timeout_seconds=30,
    side_effects=True,
    tags=("connector",),
    category="connector",
    icon_key="slack",
    idempotent=False,
    input_schema={
        "type": "object",
        "properties": {
            "webhook_url": {"type": "string", "description": "Use {\"$secret\": \"SLACK_WEBHOOK\"} — never a literal URL"},
            "url": {"type": "string", "description": "Alias for webhook_url"},
            "text": {"type": "string", "description": "Message text"},
            "channel": {"type": "string", "description": "Override channel"},
            "blocks": {"type": "array", "description": "Slack Block Kit blocks"},
        },
        "required": ["text"],
    },
    output_schema={
        "type": "object",
        "properties": {"ok": {"type": "boolean"}, "status": {"type": "integer"}},
    },
    default_policy={"timeout_seconds": 30, "max_retries": 3, "backoff": "exponential", "retry_on": "transient"},
)
def _slack_entry(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    return slack_post(payload, ctx)


@registry.register(
    "email.send",
    description="Send email through the configured SMTP server (dry-run aware)",
    timeout_seconds=120,
    side_effects=True,
    tags=("connector",),
    category="connector",
    icon_key="mail",
    idempotent=False,
    input_schema={
        "type": "object",
        "properties": {
            "to": {"description": "Recipient email or list of emails"},
            "subject": {"type": "string"},
            "text": {"type": "string", "description": "Plain-text body"},
            "html": {"type": "string", "description": "HTML body"},
        },
        "required": ["to", "subject"],
    },
    output_schema={
        "type": "object",
        "properties": {"sent": {"type": "boolean"}, "dry_run": {"type": "boolean"}},
    },
    default_policy={"timeout_seconds": 120, "max_retries": 3, "backoff": "exponential", "retry_on": "transient"},
)
def _email_entry(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    return email_send(payload, ctx)


@registry.register(
    "sql.query",
    description="Parameterized, read-only SQL against a secret connection URL",
    timeout_seconds=120,
    tags=("connector",),
    category="connector",
    icon_key="database",
    idempotent=True,
    input_schema={
        "type": "object",
        "properties": {
            "connection": {"type": "string", "description": "Connection name (use {\"$connection\": \"name\"})"},
            "sql": {"type": "string", "description": "Read-only SQL with :named parameters"},
            "params": {"type": "object", "description": "Parameter values"},
            "max_rows": {"type": "integer", "minimum": 1, "description": "Row cap"},
        },
        "required": ["sql"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "rows": {"type": "array"},
            "row_count": {"type": "integer"},
            "columns": {"type": "array", "items": {"type": "string"}},
        },
    },
    default_policy={"timeout_seconds": 120, "max_retries": 2, "backoff": "exponential"},
)
def _sql_entry(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    return sql_query(payload, ctx)


@registry.register(
    "storage.put",
    description="Store a bounded blob as this step's output (large ones become run artifacts)",
    timeout_seconds=120,
    tags=("connector",),
    category="storage",
    icon_key="save",
    idempotent=True,
    input_schema={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Storage key (run-scoped keys are idempotent)"},
            "content": {"type": "string", "description": "Text content to store"},
            "content_base64": {"type": "string", "description": "Binary content as base64"},
            "content_type": {"type": "string", "default": "application/octet-stream"},
        },
        "required": ["key"],
    },
    output_schema={
        "type": "object",
        "properties": {"key": {"type": "string"}, "bytes": {"type": "integer"}, "artifact": {"type": "boolean"}},
    },
    default_policy={"timeout_seconds": 120, "max_retries": 2, "backoff": "exponential"},
)
def _storage_put_entry(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    return storage_put(payload, ctx)


@registry.register(
    "storage.get",
    description="Read a blob stored earlier in this run",
    timeout_seconds=120,
    tags=("connector",),
    category="storage",
    icon_key="folder-open",
    idempotent=True,
    input_schema={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Storage key written by an earlier step"},
            "source_step": {"type": "string", "description": "Step ID that wrote the blob (alternative to key)"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {"key": {"type": "string"}, "content": {"type": "string"}},
    },
    default_policy={"timeout_seconds": 120, "max_retries": 2, "backoff": "exponential"},
)
def _storage_get_entry(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    return storage_get(payload, ctx)


@registry.register(
    "transform.json",
    description="Data-driven JSON mapping without evaluating any code",
    tags=("connector",),
    category="transform",
    icon_key="shuffle",
    idempotent=True,
    input_schema={
        "type": "object",
        "properties": {
            "source": {"description": "Input value (or omit to use upstream outputs)"},
            "expressions": {"type": "object", "description": "Output field -> expr.py expression mapping"},
        },
    },
    output_schema={"type": "object", "description": "Mapped object"},
    default_policy={"timeout_seconds": 60, "max_retries": 1, "backoff": "fixed"},
)
def _transform_entry(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    return transform_json(payload, ctx)


def _dependency_outputs(ctx: TaskContext) -> dict[str, Any]:
    return dict(ctx.task.get("dependency_outputs") or {})


@registry.register("demo.echo", description="Echo the input back, including upstream dependency outputs", tags=("demo",), category="demo", icon_key="echo", idempotent=True, input_schema={"type": "object"}, output_schema={"type": "object"}, default_policy={"timeout_seconds": 60, "max_retries": 1, "backoff": "fixed"})
def echo(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    ctx.log("echo handler received input", keys=sorted(payload))
    return {
        "value": payload.get("value"),
        "label": payload.get("label", ctx.step_key),
        "dependencies": _dependency_outputs(ctx),
        "attempt": ctx.attempt,
    }


@registry.register("demo.add", description="Sum a list of numbers", tags=("demo",), category="demo", icon_key="plus", idempotent=True, input_schema={"type": "object", "properties": {"numbers": {"type": "array", "items": {"type": "number"}}}}, output_schema={"type": "object", "properties": {"sum": {"type": "number"}}}, default_policy={"timeout_seconds": 60, "max_retries": 1, "backoff": "fixed"})
def add(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    values = payload.get("values") or []
    if not isinstance(values, list):
        raise ValueError("'values' must be a list of numbers")
    numbers = [float(value) for value in values]
    ctx.log(f"summing {len(numbers)} value(s)")
    return {"sum": sum(numbers), "count": len(numbers), "inputs": numbers}


@registry.register("demo.sleep", description="Sleep for a number of seconds, honouring cancellation", tags=("demo",), category="demo", icon_key="clock", idempotent=True, input_schema={"type": "object", "properties": {"seconds": {"type": "number", "minimum": 0, "maximum": 300}}}, output_schema={"type": "object", "properties": {"slept_seconds": {"type": "number"}}}, default_policy={"timeout_seconds": 330, "max_retries": 1, "backoff": "fixed"})
def sleep(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    seconds = float(payload.get("seconds", 1))
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if ctx.cancelled():
            raise RuntimeError("Cancelled while sleeping")
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    ctx.log(f"slept for {seconds:.2f}s")
    return {"slept_seconds": seconds}


@registry.register("demo.fail_once", description="Fail on the first attempt, succeed afterwards (retry demo)", tags=("demo",), category="demo", icon_key="retry", idempotent=True, input_schema={"type": "object"}, output_schema={"type": "object"}, default_policy={"timeout_seconds": 60, "max_retries": 3, "backoff": "exponential"})
def fail_once(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    if ctx.attempt <= 1:
        ctx.log("deliberate failure on the first attempt")
        raise RuntimeError(payload.get("message") or "Deliberate failure on the first attempt")
    ctx.log(f"recovered on attempt {ctx.attempt}")
    return {"recovered": True, "attempt": ctx.attempt, "value": payload.get("value")}


@registry.register(
    "demo.flaky_http",
    description="Simulate an unreliable external call with an idempotent side effect",
    side_effects=True,
    tags=("demo", "side-effect"),
    category="demo",
    icon_key="zap",
    idempotent=True,
    input_schema={
        "type": "object",
        "properties": {
            "failure_rate": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
            "always_fail": {"type": "boolean", "default": False},
            "url": {"type": "string"},
            "idempotency_key": {"type": "string"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {"status": {"type": "integer"}, "url": {"type": "string"}, "replayed": {"type": "boolean"}},
    },
    default_policy={"timeout_seconds": 60, "max_retries": 5, "backoff": "exponential"},
)
def flaky_http(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    key = payload.get("idempotency_key") or ctx.idempotency_key
    if key in _SIDE_EFFECT_LEDGER:
        ctx.log("returning the recorded result for a redelivered task", idempotency_key=key)
        return {**_SIDE_EFFECT_LEDGER[key], "replayed": True}

    failure_rate = float(payload.get("failure_rate", 0.5))
    if payload.get("always_fail"):
        raise RuntimeError("Simulated upstream outage")

    # Deterministic per (task, attempt) so behaviour is reproducible in tests.
    seed = int(hashlib.sha256(f"{key}:{ctx.attempt}".encode()).hexdigest()[:8], 16)
    roll = random.Random(seed).random()
    if roll < failure_rate:
        raise RuntimeError(f"Simulated transient failure (roll={roll:.2f}, threshold={failure_rate:.2f})")

    result = {"status": 200, "url": payload.get("url", "https://example.invalid/"), "idempotency_key": key, "attempt": ctx.attempt}
    _SIDE_EFFECT_LEDGER[key] = result
    ctx.log("side effect recorded", idempotency_key=key)
    return result


@registry.register("demo.summarize", description="Combine upstream outputs into a summary document", tags=("demo",), category="demo", icon_key="file-text", idempotent=True, input_schema={"type": "object", "properties": {"title": {"type": "string"}}}, output_schema={"type": "object", "properties": {"title": {"type": "string"}, "total": {"type": "number"}, "lines": {"type": "array", "items": {"type": "string"}}}}, default_policy={"timeout_seconds": 60, "max_retries": 1, "backoff": "fixed"})
def summarize(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    upstream = _dependency_outputs(ctx)
    title = payload.get("title", "Summary")
    lines: list[str] = []
    numbers: list[float] = []

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            if "sum" in node and isinstance(node["sum"], (int, float)):
                numbers.append(float(node["sum"]))
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(upstream)
    for key, value in sorted(upstream.items()):
        rendered = json.dumps(value, default=str, sort_keys=True)
        lines.append(f"- {key}: {rendered[:200]}")
    ctx.log(f"summarized {len(upstream)} upstream output(s)")
    return {
        "title": title,
        "upstream_steps": sorted(upstream),
        "total": round(sum(numbers), 6),
        "lines": lines,
        "workflow_input_keys": sorted((ctx.task.get("workflow_input") or {}).keys()),
        "attempt": ctx.attempt,
    }


@registry.register(
    "demo.publish_report",
    description="Pretend to publish a report, deduplicated by idempotency key",
    side_effects=True,
    tags=("demo", "side-effect"),
    category="demo",
    icon_key="send",
    idempotent=True,
    input_schema={
        "type": "object",
        "properties": {
            "channel": {"type": "string", "default": "console"},
            "body": {"description": "Report body (defaults to upstream outputs)"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {"published": {"type": "boolean"}, "channel": {"type": "string"}, "replayed": {"type": "boolean"}},
    },
    default_policy={"timeout_seconds": 60, "max_retries": 2, "backoff": "exponential"},
)
def publish_report(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    key = ctx.idempotency_key
    if key in _SIDE_EFFECT_LEDGER:
        return {**_SIDE_EFFECT_LEDGER[key], "replayed": True}
    channel = payload.get("channel", "console")
    body = payload.get("body") or _dependency_outputs(ctx)
    result = {"published": True, "channel": channel, "bytes": len(json.dumps(body, default=str)), "idempotency_key": key}
    _SIDE_EFFECT_LEDGER[key] = result
    ctx.log("report published", channel=channel)
    return result


@registry.register("demo.fail", description="Always fail — used to demonstrate terminal failures", tags=("demo",), category="demo", icon_key="x-square", idempotent=True, input_schema={"type": "object", "properties": {"message": {"type": "string"}}}, output_schema={"type": "object"}, default_policy={"timeout_seconds": 60, "max_retries": 0, "backoff": "fixed"})
def always_fail(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    raise RuntimeError(payload.get("message") or "This step is configured to fail")


__all__ = [
    "ai_classify",
    "ai_summarize",
    "always_fail",
    "add",
    "echo",
    "extract_text",
    "fail_once",
    "flaky_http",
    "publish_report",
    "sleep",
    "summarize",
]
