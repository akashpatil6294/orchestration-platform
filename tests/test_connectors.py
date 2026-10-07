"""Phase 4 connector tests: SSRF matrix, sql.query guardrails, storage, dry-run
side-effect connectors, signatures and the no-eval JSON transform."""
from __future__ import annotations

import base64
import hashlib
import hmac as hmac_module
import json
import socket
from typing import Any

import pytest

from app.worker import connector_tasks
from app.worker.connectors import (
    BlockedAddressError,
    ConnectorInputError,
    check_outbound_url,
)
from app.worker.registry import TaskContext, registry


class Context:
    """Minimal TaskContext; `redacted_keys` marks dispatch-resolved secrets."""

    attempt = 1

    def __init__(self, resolved: dict[str, Any] | None = None):
        self.task: dict[str, Any] = {"dependency_outputs": {}, "workflow_input": {}, "run_id": "run-ctx", "redacted_keys": []}
        if resolved:
            self.task["dependency_outputs"] = {}
            for path, value in (resolved or {}).items():
                self._place(path, value)
                self.task["redacted_keys"].append(path)
        self.messages: list[tuple[str, dict[str, Any]]] = []

    def _place(self, path: str, value: Any) -> None:
        node: Any = self.task
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def log(self, message: str, **fields: Any) -> None:
        self.messages.append((message, fields))

    def cancelled(self) -> bool:
        return False


def run_handler(task_type: str, payload: dict[str, Any], ctx: Context) -> dict[str, Any]:
    return registry.resolve(task_type).func(payload, ctx)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# SSRF matrix
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://10.0.0.5/x",
        "http://192.168.1.1/x",
        "http://172.16.0.9/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/x",
        "http://[fe80::1]/x",
        "http://[::ffff:127.0.0.1]/x",  # IPv4-mapped IPv6
        "http://metadata.google.internal/computeMetadata/v1/",
        "ftp://example.com/file",
    ],
)
def test_outbound_url_blocks_private_and_metadata_targets(url):
    with pytest.raises((BlockedAddressError, ConnectorInputError)):
        check_outbound_url(url, allow_private=False)


def test_outbound_url_rejects_embedded_credentials():
    with pytest.raises(ConnectorInputError, match="embedded credentials"):
        check_outbound_url("https://user:secret@example.com/x")


def test_outbound_url_enforces_the_domain_allow_list(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "connector_http_allowed_domains", ["example.com"])
    with pytest.raises(BlockedAddressError, match="ALLOWED_DOMAINS"):
        check_outbound_url("https://evil.example.net/x")


def test_dns_rebinding_is_defeated_by_resolving_for_every_hop(monkeypatch):
    """Every check re-resolves: a host that flips to a private answer is refused."""
    answers = iter(["93.184.216.34", "10.0.0.1"])

    def fake_getaddrinfo(host, *args, **kwargs):
        address = next(answers, "10.0.0.1")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    # First check sees the public answer and passes.
    check_outbound_url("http://rebinding.example/x")
    # The next hop re-resolves, sees the private answer and is blocked.
    with pytest.raises(BlockedAddressError):
        check_outbound_url("http://rebinding.example/x")


def test_resolver_guard_blocks_private_answers_at_connect_time(monkeypatch):
    """Even the HTTP client's own connect-time DNS lookup goes through the guard."""
    from app.config import settings

    monkeypatch.setattr(settings, "connector_allow_private_networks", False)
    answers = iter(["93.184.216.34", "10.0.0.1"])

    def fake_getaddrinfo(host, *args, **kwargs):
        address = next(answers, "10.0.0.1")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    from app.worker import connectors as connector_module

    connector_module.install_resolver_guard()
    try:

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def stream(self, method, url, **kwargs):
                # Simulate the HTTP client's own connect-time resolution; the
                # guard must refuse it before any socket is opened.
                host = url.split("//", 1)[1].split("/", 1)[0]
                socket.getaddrinfo(host, 80)
                raise AssertionError("the guard should have refused the resolution")

        from app.worker.connectors import ConnectorTransientError

        monkeypatch.setattr(connector_module.httpx, "Client", FakeClient)
        with pytest.raises(ConnectorTransientError, match="refused"):
            connector_module._http_call(
                "http://rebinding.example/x", method="GET", headers={}, content=None, content_type=None, timeout_seconds=5
            )
    finally:
        connector_module.uninstall_resolver_guard()


def test_redirect_to_a_private_target_is_blocked(monkeypatch):
    calls: list[str] = []

    class FakeStream:
        def __init__(self, response):
            self.response = response

        def __enter__(self):
            return self.response

        def __exit__(self, *args):
            return False

    class FakeResponse:
        status_code = 302
        headers = {"location": "http://127.0.0.1/steal"}

        def iter_bytes(self):
            yield b""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, **kwargs):
            calls.append(url)
            return FakeStream(FakeResponse())

    from app.worker import connectors as connector_module

    def public_resolution(host, *args, **kwargs):
        # Literals (like the redirect target 127.0.0.1) resolve to themselves.
        return ["93.184.216.34"] if host == "public.example" else [host]

    monkeypatch.setattr(connector_module, "resolve_host", public_resolution)
    monkeypatch.setattr(connector_module.httpx, "Client", FakeClient)
    with pytest.raises(BlockedAddressError):
        connector_module._http_call("http://public.example/x", method="GET", headers={}, content=None, content_type=None, timeout_seconds=5)
    assert calls[0] == "http://public.example/x"


def test_oversized_response_is_rejected(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "connector_http_max_response_bytes", 10)

    class FakeResponse:
        status_code = 200
        headers: dict[str, str] = {}

        def iter_bytes(self):
            yield b"x" * 100

    class FakeStream:
        def __init__(self, response):
            self.response = response

        def __enter__(self):
            return self.response

        def __exit__(self, *args):
            return False

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, **kwargs):
            return FakeStream(FakeResponse())

    from app.worker import connectors as connector_module

    monkeypatch.setattr(connector_module.httpx, "Client", FakeClient)

    def public_resolution(host, *args, **kwargs):
        return ["93.184.216.34"]

    monkeypatch.setattr(connector_module, "resolve_host", public_resolution)
    from app.worker.connectors import ConnectorPermanentError

    with pytest.raises(ConnectorPermanentError, match="limit"):
        connector_module._http_call("http://public.example/x", method="GET", headers={}, content=None, content_type=None, timeout_seconds=5)


# --------------------------------------------------------------------------- #
# http.request through the registry
# --------------------------------------------------------------------------- #
def test_http_request_validates_input_before_any_network_call():
    with pytest.raises(ConnectorInputError):
        run_handler("http.request", {"url": "http://127.0.0.1/x"}, Context())
    with pytest.raises(ConnectorInputError, match="method"):
        run_handler("http.request", {"url": "https://example.com", "method": "BREW"}, Context())


def test_http_request_requires_auth_values_to_be_secrets():
    with pytest.raises(ConnectorInputError, match="workflow secret"):
        run_handler(
            "http.request",
            {"url": "https://api.example.com", "auth": {"in": "header", "name": "Authorization", "value": "literal-token"}},
            Context(),
        )


def test_http_request_attaches_secret_auth_and_query(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_call(url, *, method, headers, content, content_type, timeout_seconds, **kwargs):
        captured.update(url=url, method=method, headers=headers)
        return {"status_code": 200, "json": {"ok": True}, "bytes": 15}

    monkeypatch.setattr(connector_tasks, "_http_call", fake_call)
    from app.worker import connectors as connector_module

    def public_resolution(host, *args, **kwargs):
        return ["93.184.216.34"]

    monkeypatch.setattr(connector_module, "resolve_host", public_resolution)
    ctx = Context(resolved={"auth.value": "supersecret"})
    result = run_handler(
        "http.request",
        {
            "url": "https://api.example.com/v1/things",
            "auth": {"in": "header", "name": "Authorization", "prefix": "Bearer ", "value": "supersecret"},
            "query": {"limit": "5"},
        },
        ctx,
    )
    assert captured["headers"]["Authorization"] == "Bearer supersecret"
    assert "limit=5" in captured["url"]
    assert result["status_code"] == 200


# --------------------------------------------------------------------------- #
# sql.query
# --------------------------------------------------------------------------- #
def test_sql_query_runs_parameterized_select_against_sqlite():
    pytest.importorskip("sqlalchemy")
    engine_url = "sqlite://"
    ctx = Context(resolved={"connection_url": engine_url})
    from sqlalchemy import create_engine, text as sql_text

    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        connection.execute(sql_text("CREATE TABLE numbers (value INTEGER)"))
        connection.execute(sql_text("INSERT INTO numbers (value) VALUES (3), (4)"))
        connection.commit()
    engine.dispose()

    # In-memory databases die with the engine; use a temp file instead.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as folder:
        db_path = Path(folder) / "connector.sqlite3"
        engine = create_engine(f"sqlite:///{db_path.as_posix()}")
        with engine.connect() as connection:
            connection.execute(sql_text("CREATE TABLE numbers (value INTEGER)"))
            connection.execute(sql_text("INSERT INTO numbers (value) VALUES (3), (4)"))
            connection.commit()
        engine.dispose()

        real_url = f"sqlite:///{db_path.as_posix()}"
        ctx = Context(resolved={"connection_url": real_url})
        result = run_handler(
            "sql.query",
            {"connection_url": real_url, "sql": "SELECT value FROM numbers WHERE value > :floor ORDER BY value", "params": {"floor": 2}},
            ctx,
        )
        assert result["columns"] == ["value"]
        assert result["rows"] == [[3], [4]]
        assert result["truncated"] is False


def test_sql_query_rejects_writes_by_default():
    with pytest.raises(ConnectorInputError, match="read-only"):
        run_handler(
            "sql.query",
            {"connection_url": "x", "sql": "DELETE FROM numbers"},
            Context(resolved={"connection_url": "sqlite:///x.db"}),
        )
    with pytest.raises(ConnectorInputError, match="single statement"):
        run_handler(
            "sql.query",
            {"connection_url": "x", "sql": "SELECT 1; SELECT 2"},
            Context(resolved={"connection_url": "sqlite:///x.db"}),
        )
    with pytest.raises(ConnectorInputError, match="workflow secret"):
        run_handler("sql.query", {"connection_url": "sqlite:///literal.db", "sql": "SELECT 1"}, Context())


# --------------------------------------------------------------------------- #
# storage.put / storage.get
# --------------------------------------------------------------------------- #
def test_storage_roundtrip_rejects_traversal_and_cross_run_reads():
    stored = run_handler("storage.put", {"key": "reports/2026.json", "content": "{\"total\": 450}", "content_type": "application/json"}, Context())
    assert stored["key"].startswith("runs/run-ctx/")
    assert stored["bytes"] > 0

    with pytest.raises(ConnectorInputError, match="key"):
        run_handler("storage.put", {"key": "../escape", "content": "x"}, Context())
    with pytest.raises(ConnectorInputError, match="key"):
        run_handler("storage.put", {"key": "/absolute", "content": "x"}, Context())

    other_run = Context()
    other_run.task["run_id"] = "run-other"
    other_run.task["dependency_outputs"] = {"save": stored}
    with pytest.raises(ConnectorInputError, match="does not belong"):
        run_handler("storage.get", {"source_step": "save"}, other_run)

    mine = Context()
    mine.task["dependency_outputs"] = {"save": stored}
    fetched = run_handler("storage.get", {"source_step": "save"}, mine)
    assert fetched["content_base64"] == "{\"total\": 450}"


# --------------------------------------------------------------------------- #
# Side-effect connectors: dry run, allow-list and signatures
# --------------------------------------------------------------------------- #
def test_email_send_dry_run_never_touches_smtp(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "connectors_dry_run", True)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    result = run_handler("email.send", {"to": "ops@example.com", "subject": "Hi", "text": "Body"}, Context())
    assert result["simulated"] is True


def test_email_send_requires_a_configured_server(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "smtp_host", "")
    with pytest.raises(Exception, match="SMTP_HOST"):
        run_handler("email.send", {"to": "ops@example.com", "subject": "Hi", "text": "Body"}, Context())


def test_email_send_rejects_header_injection():
    with pytest.raises(ConnectorInputError, match="subject"):
        run_handler("email.send", {"to": "ops@example.com", "subject": "Hi\r\nBcc: victim@example.com", "text": "Body"}, Context())


def test_slack_post_is_restricted_to_allowed_hosts():
    with pytest.raises(ConnectorInputError, match="ALLOWED_HOSTS"):
        run_handler("slack.post", {"webhook_url": "https://evil.example/hook", "text": "hi"}, Context())


def test_webhook_call_signs_the_body_with_the_secret(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_call(url, *, method, headers, content, content_type, timeout_seconds, **kwargs):
        captured.update(url=url, headers=headers, content=content)
        return {"status_code": 200, "bytes": len(content or b"")}

    monkeypatch.setattr(connector_tasks, "_http_call", fake_call)
    from app.worker import connectors as connector_module

    def public_resolution(host, *args, **kwargs):
        return ["93.184.216.34"]

    monkeypatch.setattr(connector_module, "resolve_host", public_resolution)
    ctx = Context(resolved={"signing_secret": "s3cr3t-value"})
    run_handler("webhook.call", {"url": "https://hooks.example/x", "json": {"a": 1}, "signing_secret": "s3cr3t-value"}, ctx)

    timestamp = captured["headers"]["X-Orchestrator-Timestamp"]
    expected = hmac_module.new(b"s3cr3t-value", f"{timestamp}.".encode() + captured["content"], hashlib.sha256).hexdigest()
    assert captured["headers"]["X-Orchestrator-Signature"] == f"sha256={expected}"


def test_webhook_call_dry_run_sends_nothing(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "connectors_dry_run", True)
    result = run_handler("webhook.call", {"url": "https://hooks.example/x", "body": "hi", "signing_secret": "x"}, Context(resolved={"signing_secret": "x"}))
    assert result["simulated"] is True


# --------------------------------------------------------------------------- #
# transform.json
# --------------------------------------------------------------------------- #
def test_transform_json_supports_pick_map_filter_merge_without_eval():
    source = {
        "invoice": {"total": 450, "currency": "USD"},
        "lines": [{"kind": "labor", "amount": 300}, {"kind": "parts", "amount": 150}],
        "meta": {"issued_by": "ACME"},
    }
    result = run_handler(
        "transform.json",
        {
            "source": source,
            "expressions": {
                "total": "$invoice.total",
                "currency": "invoice.currency",
                "labor": {"filter": "lines", "where": {"path": "$item.kind", "equals": "labor"}},
                "names": {"map": {"kind": "$item.kind"}, "select": "lines"},
                "header": {"merge": ["$invoice", "$meta"]},
                "greeting": {"literal": "hello"},
            },
        },
        Context(),
    )
    assert result["total"] == 450
    assert result["currency"] == "USD"
    assert result["labor"] == [{"kind": "labor", "amount": 300}]
    assert result["names"] == [{"kind": "labor"}, {"kind": "parts"}]
    assert result["header"]["issued_by"] == "ACME"
    assert result["greeting"] == "hello"


def test_transform_json_never_evaluates_code():
    with pytest.raises(ConnectorInputError):
        run_handler(
            "transform.json",
            {"source": {}, "expressions": {"x": "__import__('os').system('echo pwned')"}},
            Context(),
        )
    with pytest.raises(ConnectorInputError, match="Unsupported path"):
        run_handler(
            "transform.json",
            {"source": {"a": 1}, "expressions": {"x": "$a; import os"}},
            Context(),
        )
