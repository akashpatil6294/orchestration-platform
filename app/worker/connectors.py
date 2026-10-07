"""Allow-listed connectors executed by workers.

Every connector is a plain task handler: workflows name a task type, never code.
Handlers validate their own input, never retry internally (the platform's retry
policy and the step's idempotency key own that), and classify failures as
retryable or permanent so the Phase 3 circuit breaker only counts real transients.

Outbound HTTP (``http.request``, ``slack.post``, ``webhook.call``) resolves and
validates every hop:

* only ``http``/``https``, no embedded credentials;
* private, loopback, link-local, multicast, reserved and unspecified addresses
  are refused, including IPv4-mapped IPv6 and cloud metadata endpoints;
* DNS is resolved and every returned address is checked, and the whole check is
  repeated on each redirect;
* an optional ``CONNECTOR_HTTP_ALLOWED_DOMAINS`` allow-list restricts hosts;
* redirect count, request size and response size are bounded.

Residual risk: the check and the connection are separate syscalls, so a DNS
rebinding between them is theoretically possible; run workers in a network that
cannot reach link-local metadata, and set an allow-list for untrusted tenants.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
from contextlib import contextmanager
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import settings
from app.worker.registry import TaskContext

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
SAFE_RESPONSE_HEADERS = {
    "content-type",
    "content-length",
    "retry-after",
    "etag",
    "last-modified",
    "location",
    "x-request-id",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
}
CLOUD_METADATA_HOSTS = {
    "metadata.google.internal",
    "metadata.goog",
    "metadata.azure.com",
    "169.254.169.254",
}


class ConnectorInputError(ValueError):
    """The workflow supplied an input the connector refuses. Never retried."""

    retryable = False


class ConnectorPermanentError(RuntimeError):
    """The remote system rejected the request in a way retrying cannot fix."""

    retryable = False


class ConnectorTransientError(RuntimeError):
    """A timeout, network problem, rate limit or server error. Safe to retry."""

    retryable = True


class BlockedAddressError(ConnectorInputError):
    """The target resolves to an address the platform refuses to call."""


def _bounded_string(value: Any, *, field: str, max_length: int, required: bool = True, default: str = "") -> str:
    if value is None:
        if required:
            raise ConnectorInputError(f"'{field}' is required")
        return default
    if not isinstance(value, str):
        raise ConnectorInputError(f"'{field}' must be text")
    text = value.strip()
    if required and not text:
        raise ConnectorInputError(f"'{field}' must not be empty")
    if len(text) > max_length:
        raise ConnectorInputError(f"'{field}' exceeds the {max_length}-character limit")
    return text


def _bounded_int(value: Any, *, field: str, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConnectorInputError(f"'{field}' must be an integer")
    if not minimum <= value <= maximum:
        raise ConnectorInputError(f"'{field}' must be between {minimum} and {maximum}")
    return value


def _string_map(value: Any, *, field: str, max_entries: int = 64, max_length: int = 4000) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConnectorInputError(f"'{field}' must be an object of string values")
    if len(value) > max_entries:
        raise ConnectorInputError(f"'{field}' accepts at most {max_entries} entries")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise ConnectorInputError(f"'{field}' contains an invalid header name")
        if "\n" in key or "\r" in key or not isinstance(item, str) or "\n" in item or "\r" in item:
            raise ConnectorInputError(f"'{field}' contains a header that cannot be sent")
        if len(item) > max_length:
            raise ConnectorInputError(f"'{field}.{key}' exceeds the {max_length}-character limit")
        result[key.strip()] = item
    return result


def secret_resolved_paths(ctx: TaskContext) -> set[str]:
    return {str(path) for path in (ctx.task.get("redacted_keys") or [])}


def require_secret_field(payload: dict[str, Any], ctx: TaskContext, field: str, *, alternative: str | None = None) -> str:
    """Return a field that must have been resolved from a workflow secret.

    Literal credentials inside a definition would be published in plaintext, so
    connectors that need a credential insist on a ``{"$secret": ...}`` (or
    ``{{secrets.NAME}}``) reference, which dispatch resolves only for the worker.
    Dispatch records redacted paths relative to the step input root, so a local
    field matches when the recorded path ends with it (``auth.value`` for a
    field ``value`` inside an ``auth`` object, ``connection_url`` at the root).
    """
    value = payload.get(field)
    if value is None and alternative:
        field = alternative
        value = payload.get(field)
    resolved = secret_resolved_paths(ctx)
    if not any(path == field or path.endswith(f".{field}") for path in resolved):
        raise ConnectorInputError(
            f"'{field}' must reference a workflow secret (use {{\"$secret\": \"NAME\"}} or {{secrets.NAME}})"
        )
    return _bounded_string(value, field=field, max_length=4000)


def _blocked_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _blocked_ip(address.ipv4_mapped)
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def resolve_host(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ConnectorTransientError(f"Could not resolve '{host}' ({exc.__class__.__name__})") from exc
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise ConnectorTransientError(f"Could not resolve '{host}'")
    return addresses


def check_outbound_url(url: str, *, allow_private: bool | None = None) -> str:
    """Validate a single outbound URL, resolving DNS and checking every address."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ConnectorInputError("Only http and https URLs are supported")
    if parts.username or parts.password:
        raise ConnectorInputError("URLs with embedded credentials are not allowed; use a secret reference")
    host = parts.hostname
    if not host:
        raise ConnectorInputError("The URL must include a host")
    if host.lower() in CLOUD_METADATA_HOSTS:
        raise BlockedAddressError("Cloud metadata endpoints are blocked")
    allow_private = settings.connector_allow_private_networks if allow_private is None else allow_private
    allowed_domains = [domain.lower() for domain in settings.connector_http_allowed_domains]
    if allowed_domains and not any(host.lower() == domain or host.lower().endswith(f".{domain}") for domain in allowed_domains):
        raise BlockedAddressError(f"Host '{host}' is not in CONNECTOR_HTTP_ALLOWED_DOMAINS")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    extra_networks = [ipaddress.ip_network(item, strict=False) for item in settings.connector_blocked_networks]
    for address in resolve_host(host, port):
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:  # pragma: no cover - getaddrinfo returns literals
            raise ConnectorTransientError(f"Could not parse resolved address for '{host}'") from exc
        if _blocked_ip(parsed) and not allow_private:
            raise BlockedAddressError(f"Host '{host}' resolves to a blocked address")
        if any(parsed in network for network in extra_networks):
            raise BlockedAddressError(f"Host '{host}' resolves to an address blocked by configuration")
    return url


_resolver_guard_installed = False
_resolver_guard_original: Any = None


def install_resolver_guard(exempt_hosts: set[str] | None = None) -> None:
    """Validate every DNS resolution in this process, defeating DNS rebinding.

    ``check_outbound_url`` re-resolves per hop, but the HTTP client performs its
    own resolution at connect time. Installing the guard wraps
    ``socket.getaddrinfo`` so those connect-time resolutions are checked too:
    a host that flip-flops between a public and a private address fails on the
    private answer, no matter which layer asked. The orchestrator API host is
    exempt (workers legitimately connect to it, often on loopback).
    """
    global _resolver_guard_installed, _resolver_guard_original
    if _resolver_guard_installed:
        return
    exempt = {host.lower() for host in (exempt_hosts or set())}
    _resolver_guard_original = socket.getaddrinfo

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any):
        infos = _resolver_guard_original(host, *args, **kwargs)
        if settings.connector_allow_private_networks or not isinstance(host, str) or host.lower() in exempt:
            return infos
        extra_networks = [ipaddress.ip_network(item, strict=False) for item in settings.connector_blocked_networks]
        for info in infos:
            try:
                parsed = ipaddress.ip_address(info[4][0])
            except (ValueError, TypeError):
                continue
            if _blocked_ip(parsed) or any(parsed in network for network in extra_networks):
                raise socket.gaierror(11001, f"Host '{host}' resolved to a blocked address")
        return infos

    socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]
    _resolver_guard_installed = True


def uninstall_resolver_guard() -> None:
    """Test hook: restore the original resolver."""
    global _resolver_guard_installed, _resolver_guard_original
    if not _resolver_guard_installed:
        return
    if _resolver_guard_original is not None:
        socket.getaddrinfo = _resolver_guard_original  # type: ignore[assignment]
    _resolver_guard_original = None
    _resolver_guard_installed = False


def _request_body(payload: dict[str, Any]) -> tuple[bytes | None, str | None]:
    """Return (content, content_type) for the outbound body."""
    supplied = [key for key in ("json", "body", "form") if payload.get(key) is not None]
    if len(supplied) > 1:
        raise ConnectorInputError("Provide only one of 'json', 'body' or 'form'")
    if not supplied:
        return None, None
    key = supplied[0]
    if key == "json":
        try:
            content = json.dumps(payload["json"], default=str).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ConnectorInputError("'json' must be JSON-serialisable") from exc
        return content, "application/json"
    if key == "form":
        form = _string_map(payload["form"], field="form")
        return httpx.QueryParams(form).encode().encode("utf-8") if False else "&".join(
            f"{httpx.QueryParams({name: value})}" for name, value in form.items()
        ).encode("utf-8"), "application/x-www-form-urlencoded"
    body = payload["body"]
    if not isinstance(body, str):
        raise ConnectorInputError("'body' must be text")
    encoded = body.encode("utf-8")
    if len(encoded) > settings.connector_http_max_request_bytes:
        raise ConnectorInputError(
            f"Request body exceeds the {settings.connector_http_max_request_bytes}-byte limit"
        )
    return encoded, "text/plain"


@contextmanager
def _bracket_safe_no_proxy():
    """Temporarily strip brackets from IPv6 literals in no_proxy.

    ``httpx`` parses ``no_proxy`` entries with ``URLPattern``; a bracketed
    literal such as ``[::1]`` (common in container runtimes) makes client
    construction raise ``InvalidURL``. Unbracketed ``::1`` is recognized by
    ``is_ipv6_hostname`` and works. The environment is restored afterwards.
    """
    keys = [key for key in ("no_proxy", "NO_PROXY") if key in os.environ]
    saved = {key: os.environ[key] for key in keys}
    try:
        for key in keys:
            parts = []
            for part in os.environ[key].split(","):
                piece = part.strip()
                if piece.startswith("[") and piece.endswith("]") and len(piece) > 2:
                    piece = piece[1:-1]
                parts.append(piece)
            os.environ[key] = ",".join(parts)
        yield
    finally:
        for key, value in saved.items():
            os.environ[key] = value


def _http_call(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    content: bytes | None,
    content_type: str | None,
    timeout_seconds: int,
    max_redirects: int | None = None,
    allow_private: bool | None = None,
) -> dict[str, Any]:
    """Perform an outbound request with per-hop SSRF validation and bounded I/O."""
    if method not in METHODS:
        raise ConnectorInputError(f"Unsupported HTTP method '{method}'")
    if content is not None and len(content) > settings.connector_http_max_request_bytes:
        raise ConnectorInputError(
            f"Request body exceeds the {settings.connector_http_max_request_bytes}-byte limit"
        )
    with _bracket_safe_no_proxy():
        return _http_call_with_client(
            url,
            method=method,
            headers=headers,
            content=content,
            content_type=content_type,
            timeout_seconds=timeout_seconds,
            max_redirects=max_redirects,
            allow_private=allow_private,
        )


def _http_call_with_client(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    content: bytes | None,
    content_type: str | None,
    timeout_seconds: int,
    max_redirects: int | None,
    allow_private: bool | None,
) -> dict[str, Any]:
    redirects = settings.connector_http_max_redirects if max_redirects is None else max_redirects
    current = url
    current_method = method
    current_body = content
    current_type = content_type
    hops = 0
    while True:
        check_outbound_url(current, allow_private=allow_private)
        request_headers = dict(headers)
        if current_body is not None and current_type:
            request_headers.setdefault("Content-Type", current_type)
        try:
            with httpx.Client(follow_redirects=False, timeout=timeout_seconds) as client:
                with client.stream(current_method, current, headers=request_headers, content=current_body) as response:
                    if response.status_code in REDIRECT_STATUSES and "location" in response.headers:
                        if hops >= redirects:
                            raise ConnectorPermanentError(
                                f"Too many redirects (limit {redirects})"
                            )
                        hops += 1
                        location = response.headers["location"]
                        current = urljoin(current, location)
                        if response.status_code in {301, 302, 303} and current_method != "HEAD":
                            current_method = "GET"
                            current_body = None
                            current_type = None
                        continue
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > settings.connector_http_max_response_bytes:
                            raise ConnectorPermanentError(
                                f"Response exceeded the {settings.connector_http_max_response_bytes}-byte limit"
                            )
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    status_code = response.status_code
                    response_headers = {
                        name.lower(): value
                        for name, value in response.headers.items()
                        if name.lower() in SAFE_RESPONSE_HEADERS
                    }
        except httpx.TimeoutException as exc:
            raise ConnectorTransientError(f"Request to '{urlsplit(current).hostname}' timed out") from exc
        except socket.gaierror as exc:
            raise ConnectorTransientError(f"DNS resolution refused for '{urlsplit(current).hostname}'") from exc
        except httpx.HTTPError as exc:
            raise ConnectorTransientError(f"Request failed ({exc.__class__.__name__})") from exc
        except (ConnectorInputError, ConnectorPermanentError, ConnectorTransientError):
            raise

        if status_code >= 500 or status_code in {408, 425, 429}:
            raise ConnectorTransientError(f"Remote returned HTTP {status_code}")
        if status_code >= 400:
            raise ConnectorPermanentError(f"Remote returned HTTP {status_code}")
        return _decode_response(status_code, response_headers, body)


def _decode_response(status_code: int, response_headers: dict[str, str], body: bytes) -> dict[str, Any]:
    content_type = response_headers.get("content-type", "")
    result: dict[str, Any] = {
        "status_code": status_code,
        "headers": response_headers,
        "bytes": len(body),
    }
    text = body.decode("utf-8", "replace")
    if "json" in content_type and text.strip():
        try:
            result["json"] = json.loads(text)
            return result
        except ValueError:
            pass
    limit = settings.connector_http_max_text_chars
    result["text"] = text[:limit]
    result["truncated"] = len(text) > limit
    return result


__all__ = [
    "BlockedAddressError",
    "CLOUD_METADATA_HOSTS",
    "ConnectorInputError",
    "ConnectorPermanentError",
    "ConnectorTransientError",
    "METHODS",
    "SAFE_RESPONSE_HEADERS",
    "_bounded_int",
    "_bounded_string",
    "_http_call",
    "_request_body",
    "_string_map",
    "check_outbound_url",
    "install_resolver_guard",
    "uninstall_resolver_guard",
    "require_secret_field",
    "resolve_host",
    "secret_resolved_paths",
]
