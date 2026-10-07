"""Registered connector handlers.

Every connector is a plain function registered in ``handlers.py`` (the single registration surface), built on the shared guards in
``connectors.py``: bounded inputs, per-hop SSRF validation for outbound HTTP,
secrets that must come from workflow secrets (never literal values), retryable
vs permanent error classification, and dry-run support for side-effecting
connectors. Workers execute only what is registered here; a workflow can name a
connector, never supply code.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac as hmac_module
import json
import re
import smtplib
import time
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import create_engine, text as sql_text

from app.config import settings
from app.worker.connectors import (
    CLOUD_METADATA_HOSTS,
    ConnectorInputError,
    ConnectorPermanentError,
    ConnectorTransientError,
    BlockedAddressError,
    _bounded_int,
    _bounded_string,
    _http_call,
    _request_body,
    _string_map,
    check_outbound_url,
    require_secret_field,
)
from app.worker.registry import TaskContext

STORAGE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
SQL_ALLOWED_PREFIXES = ("select", "with", "explain", "show", "values")


def _query_params(payload: dict[str, Any]) -> dict[str, str]:
    return _string_map(payload.get("query"), field="query")


def _auth_headers(payload: dict[str, Any], ctx: TaskContext) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve `auth` into headers/query values; the value must be a secret."""
    auth = payload.get("auth")
    if auth is None:
        return {}, {}
    if not isinstance(auth, dict):
        raise ConnectorInputError("'auth' must be an object")
    location = auth.get("in", "header")
    if location not in {"header", "query"}:
        raise ConnectorInputError("'auth.in' must be 'header' or 'query'")
    name = _bounded_string(auth.get("name"), field="auth.name", max_length=128)
    if "\n" in name or "\r" in name:
        raise ConnectorInputError("'auth.name' cannot contain newlines")
    value = require_secret_field(auth, ctx, field="value")
    prefix = auth.get("prefix")
    if prefix is not None and (not isinstance(prefix, str) or len(prefix) > 64):
        raise ConnectorInputError("'auth.prefix' must be a short string")  # trailing spaces are meaningful
    if location == "header":
        return {name: f"{prefix}{value}"}, {}
    return {}, {name: value}


def http_request(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    url = _bounded_string(payload.get("url"), field="url", max_length=2000)
    method = _bounded_string(payload.get("method", "GET"), field="method", max_length=10).upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        raise ConnectorInputError("'method' must be one of GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS")
    headers = _string_map(payload.get("headers"), field="headers")
    auth_headers, auth_query = _auth_headers(payload, ctx)
    headers.update(auth_headers)
    params = _query_params(payload)
    params.update(auth_query)
    if params:
        separator = "&" if "?" in url else "?"
        encoded = "&".join(f"{httpx.QueryParams({name: value})}" for name, value in params.items())
        url = f"{url}{separator}{encoded}"
    check_outbound_url(url)
    content, content_type = _request_body(payload)
    timeout = _bounded_int(payload.get("timeout_seconds"), field="timeout_seconds", default=settings.connector_http_timeout_seconds, minimum=1, maximum=300)
    result = _http_call(url, method=method, headers=headers, content=content, content_type=content_type, timeout_seconds=timeout)
    result["attempt"] = ctx.attempt
    return result


def webhook_call(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    url = _bounded_string(payload.get("url"), field="url", max_length=2000)
    method = _bounded_string(payload.get("method", "POST"), field="method", max_length=10).upper()
    headers = _string_map(payload.get("headers"), field="headers")
    signing_secret = require_secret_field(payload, ctx, field="signing_secret")

    body_payload = {"json": payload["json"]} if payload.get("json") is not None else ({"body": payload["body"]} if payload.get("body") is not None else {})
    content, content_type = _request_body(body_payload)
    if content is None:
        content = b""

    if settings.connectors_dry_run:
        # Dry run validates the URL shape and signs nothing: no DNS, no send.
        parts_dry = urlsplit(url)
        if parts_dry.scheme not in {"http", "https"} or not parts_dry.hostname:
            raise ConnectorInputError("A webhook URL needs an http(s) scheme and a host")
        ctx.log("webhook.call dry run: not sending", url_host=parts_dry.hostname, bytes=len(content))
        return {"simulated": True, "status_code": 0, "bytes": len(content), "attempt": ctx.attempt}

    check_outbound_url(url)
    timestamp = str(int(time.time()))
    mac = hmac_module.new(signing_secret.encode(), f"{timestamp}.".encode() + content, hashlib.sha256).hexdigest()
    headers["X-Orchestrator-Timestamp"] = timestamp
    headers["X-Orchestrator-Signature"] = f"sha256={mac}"
    if content and content_type:
        headers.setdefault("Content-Type", content_type)
    result = _http_call(url, method=method, headers=headers, content=content, content_type=None, timeout_seconds=settings.connector_http_timeout_seconds)
    result["attempt"] = ctx.attempt
    return result


def slack_post(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    url = _bounded_string(payload.get("webhook_url") or payload.get("url"), field="webhook_url", max_length=2000)
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in {host.lower() for host in settings.connector_slack_allowed_hosts}:
        raise ConnectorInputError("The webhook host must be one of CONNECTOR_SLACK_ALLOWED_HOSTS over https")
    text_value = _bounded_string(payload.get("text"), field="text", max_length=4000)
    body: dict[str, Any] = {"text": text_value}
    if payload.get("channel"):
        body["channel"] = _bounded_string(payload.get("channel"), field="channel", max_length=80)
    if settings.connectors_dry_run:
        ctx.log("slack.post dry run: not sending", host=parts.hostname, characters=len(text_value))
        return {"simulated": True, "status_code": 200, "attempt": ctx.attempt}
    result = _http_call(url, method="POST", headers={}, content=json.dumps(body).encode(), content_type="application/json", timeout_seconds=settings.connector_slack_timeout_seconds)
    result["attempt"] = ctx.attempt
    return result


def email_send(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    recipients_raw = payload.get("to")
    if isinstance(recipients_raw, str):
        recipients_raw = [recipients_raw]
    if not isinstance(recipients_raw, list) or not recipients_raw:
        raise ConnectorInputError("'to' must be a recipient or a list of recipients")
    if len(recipients_raw) > settings.connector_email_recipient_limit:
        raise ConnectorInputError(f"'to' exceeds the {settings.connector_email_recipient_limit}-recipient limit")
    recipients = []
    for recipient in recipients_raw:
        address = _bounded_string(recipient, field="to", max_length=320)
        if "@" not in address or "\n" in address or "\r" in address:
            raise ConnectorInputError(f"'{address}' is not a valid recipient address")
        recipients.append(address)
    subject = _bounded_string(payload.get("subject"), field="subject", max_length=500)
    if "\n" in subject or "\r" in subject:
        raise ConnectorInputError("'subject' cannot contain newlines")
    body = _bounded_string(payload.get("text"), field="text", max_length=200_000)

    if settings.connectors_dry_run:
        ctx.log("email.send dry run: not sending", recipients=len(recipients), characters=len(body))
        return {"simulated": True, "recipients": recipients, "attempt": ctx.attempt}

    if not settings.smtp_host:
        raise ConnectorPermanentError("SMTP_HOST is not configured for this worker")
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds) as server:
            if settings.smtp_starttls:
                server.starttls()
            if settings.smtp_username or settings.smtp_password:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise ConnectorTransientError(f"Could not send the email ({exc.__class__.__name__})") from exc
    ctx.log("email sent", recipients=len(recipients))
    return {"sent": True, "recipients": recipients, "attempt": ctx.attempt}


def sql_query(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    connection_url = require_secret_field(payload, ctx, field="connection_url")
    statement = _bounded_string(payload.get("sql"), field="sql", max_length=20_000)
    params = payload.get("params") or {}
    if not isinstance(params, (list, dict)):
        raise ConnectorInputError("'params' must be a list or an object")
    max_rows = _bounded_int(payload.get("max_rows"), field="max_rows", default=settings.connector_sql_max_rows, minimum=1, maximum=settings.connector_sql_max_rows)
    _ensure_read_only(statement)
    if ";" in statement.rstrip().rstrip(";"):
        raise ConnectorInputError("Only a single statement can be executed")

    from sqlalchemy.exc import DBAPIError, SQLAlchemyError

    engine = create_engine(connection_url, connect_args=_connect_args())
    try:
        with engine.connect() as connection:
            if engine.dialect.name == "postgresql":
                connection.exec_driver_sql(f"SET LOCAL statement_timeout = {int(settings.connector_sql_timeout_seconds) * 1000}")
            started = time.monotonic()
            result = connection.execute(sql_text(statement), params if isinstance(params, dict) else tuple(params))
            if result.returns_rows:
                rows = result.fetchmany(max_rows + 1)
            else:
                rows = []
            elapsed = round(time.monotonic() - started, 3)
    except DBAPIError as exc:
        raise ConnectorTransientError(f"Query failed ({exc.__class__.__name__})") from exc
    except SQLAlchemyError as exc:
        raise ConnectorTransientError(f"Query failed ({exc.__class__.__name__})") from exc
    finally:
        engine.dispose()

    truncated = len(rows) > max_rows
    visible = rows[:max_rows]
    columns = list(visible[0]._mapping.keys()) if visible else []
    return {
        "columns": columns,
        "rows": [[value if isinstance(value, (str, int, float, bool, type(None))) else str(value) for value in row] for row in visible],
        "row_count": len(visible),
        "truncated": truncated,
        "elapsed_seconds": elapsed,
        "attempt": ctx.attempt,
    }


def _connect_args() -> dict[str, Any]:
    return {"timeout": settings.connector_sql_timeout_seconds}


def _ensure_read_only(statement: str) -> None:
    """Reject non-reading statements unless an admin enabled writes."""
    if settings.connector_sql_allow_writes:
        return
    stripped = re.sub(r"--[^\n]*", " ", statement)
    stripped = re.sub(r"/\*.*?\*/", " ", stripped, flags=re.DOTALL)
    first_word = stripped.lstrip(" \t\r\n(").split(" ", 1)[0].lower() if stripped.strip() else ""
    if first_word not in SQL_ALLOWED_PREFIXES:
        raise ConnectorInputError("Only read-only statements (SELECT/WITH/EXPLAIN/SHOW/VALUES) are allowed")


def storage_put(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    """Store a blob for this run by emitting it as the step output.

    The platform persists step outputs through the artifact service (large
    payloads become authenticated, owner-scoped run artifacts), so the worker
    stays stateless and every read is scoped to the owning run.
    """
    key = _storage_key(payload.get("key"))
    content_type = _bounded_string(payload.get("content_type", "application/octet-stream"), field="content_type", max_length=120)
    encoded = payload.get("content_base64")
    text_content = payload.get("content")
    if (encoded is None) == (text_content is None):
        raise ConnectorInputError("Provide exactly one of 'content' (text) or 'content_base64'")
    if encoded is not None:
        if not isinstance(encoded, str):
            raise ConnectorInputError("'content_base64' must be base64 text")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ConnectorInputError("'content_base64' is not valid base64") from exc
        body: str = encoded
    else:
        if not isinstance(text_content, str):
            raise ConnectorInputError("'content' must be text")
        content = text_content.encode("utf-8")
        body = text_content
    if not content or len(content) > settings.connector_storage_max_bytes:
        raise ConnectorInputError(f"Content must be between 1 byte and {settings.connector_storage_max_bytes} bytes")
    return {
        "storage": "run",
        "key": f"runs/{ctx.task.get('run_id')}/{key}",
        "content_base64": body,
        "content_type": content_type,
        "bytes": len(content),
        "attempt": ctx.attempt,
    }


def storage_get(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    source_step = _bounded_string(payload.get("source_step"), field="source_step", max_length=128)
    dependency = (ctx.task.get("dependency_outputs") or {}).get(source_step)
    if not isinstance(dependency, dict):
        raise ConnectorInputError(f"Dependency '{source_step}' did not provide a stored blob")
    if dependency.get("storage") != "run":
        raise ConnectorInputError(f"Dependency '{source_step}' is not a stored blob")
    expected_prefix = f"runs/{ctx.task.get('run_id')}/"
    key = dependency.get("key")
    if not isinstance(key, str) or not key.startswith(expected_prefix):
        raise ConnectorInputError("The stored blob does not belong to this run")
    return {
        "key": key[len(expected_prefix):],
        "content_base64": dependency.get("content_base64"),
        "content_type": dependency.get("content_type"),
        "bytes": dependency.get("bytes"),
        "attempt": ctx.attempt,
    }


def _storage_key(value: Any) -> str:
    key = _bounded_string(value, field="key", max_length=200)
    if not STORAGE_KEY_PATTERN.match(key) or ".." in key:
        raise ConnectorInputError("'key' must be a relative path of letters, digits, dots, dashes, slashes or underscores")
    return key


def transform_json(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    source = payload.get("source")
    if not isinstance(source, dict):
        raise ConnectorInputError("'source' must be an object")
    expressions = payload.get("expressions")
    if not isinstance(expressions, dict) or not expressions:
        raise ConnectorInputError("'expressions' must be a non-empty object")
    result: dict[str, Any] = {}
    for output_key, rule in expressions.items():
        if not isinstance(output_key, str) or len(output_key) > 120:
            raise ConnectorInputError("Expression keys must be short strings")
        result[output_key] = _evaluate_rule(rule, source, depth=0)
    return {**result, "attempt": ctx.attempt}


_MAX_TRANSFORM_DEPTH = 8


def _pick(source: Any, path: str) -> Any:
    if not isinstance(path, str):
        raise ConnectorInputError("Paths must be strings")
    if path in {"", "$"}:
        return source
    node = source
    for token in path.lstrip("$.").split("."):
        match = re.fullmatch(r"([A-Za-z0-9_-]+)(?:\[(\d+)\])?", token)
        if not match:
            raise ConnectorInputError(f"Unsupported path segment '{token}'")
        name, index = match.group(1), match.group(2)
        if isinstance(node, dict) and name in node:
            node = node[name]
        else:
            return None
        if index is not None:
            if not isinstance(node, list) or int(index) >= len(node):
                return None
            node = node[int(index)]
    return node


def _evaluate_rule(rule: Any, source: Any, *, depth: int) -> Any:
    if depth > _MAX_TRANSFORM_DEPTH:
        raise ConnectorInputError("Transform nesting is too deep")
    if isinstance(rule, str):
        return _pick(source, rule)
    if isinstance(rule, (int, float, bool)) or rule is None:
        return rule
    if not isinstance(rule, dict):
        raise ConnectorInputError("Rules must be paths, literals or operation objects")
    keys = set(rule.keys())
    if "path" in keys and keys <= {"path"}:
        return _pick(source, rule["path"])
    if "merge" in keys and keys == {"merge"}:
        if not isinstance(rule["merge"], list) or len(rule["merge"]) > 32:
            raise ConnectorInputError("'merge' must be a list of paths or objects")
        merged: dict[str, Any] = {}
        for item in rule["merge"]:
            value = _evaluate_rule(item, source, depth=depth + 1)
            if not isinstance(value, dict):
                raise ConnectorInputError("'merge' combines only objects")
            merged.update(value)
        return merged
    if "map" in keys and keys == {"map", "select"}:
        mapping = rule["map"]
        items = _pick(source, rule["select"])
        if not isinstance(items, list):
            raise ConnectorInputError("'select' must point at a list")
        if len(items) > 10_000:
            raise ConnectorInputError("'map' is limited to 10,000 items")
        output = []
        for item in items:
            scoped = {"item": item, **(source if isinstance(source, dict) else {})}
            entry = {key: _evaluate_rule(inner, scoped, depth=depth + 1) for key, inner in mapping.items()}
            output.append(entry)
        return output
    if "filter" in keys and keys == {"filter", "where"}:
        items = _pick(source, rule["filter"])
        if not isinstance(items, list):
            raise ConnectorInputError("'filter' must point at a list")
        where = rule["where"]
        if not isinstance(where, dict) or not where.get("path"):
            raise ConnectorInputError("'where' must reference a path")
        expected = where.get("equals", True)
        kept = []
        for item in items:
            scoped = {"item": item, **(source if isinstance(source, dict) else {})}
            if _pick(scoped, where["path"]) == expected:
                kept.append(item)
        return kept
    if keys <= {"literal"}:
        return rule["literal"]
    raise ConnectorInputError("Unsupported rule; use 'path', 'map'+'select', 'filter'+'where', 'merge' or 'literal'")


__all__ = [
    "BlockedAddressError",
    "CLOUD_METADATA_HOSTS",
    "ConnectorInputError",
    "ConnectorPermanentError",
    "ConnectorTransientError",
    "email_send",
    "http_request",
    "slack_post",
    "sql_query",
    "storage_get",
    "storage_put",
    "transform_json",
    "webhook_call",
]
