"""Operational metrics.

A tiny thread-safe registry rather than a client library: the counters the
platform needs are few, and this keeps the dependency surface small. Exposed in
Prometheus text exposition format from ``GET /metrics``.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Iterable

_LOCK = threading.Lock()
_COUNTERS: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
_GAUGES: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
_HISTOGRAMS: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(list)
_STARTED_AT = time.time()

_HELP: dict[str, tuple[str, str]] = {
    "orchestrator_http_requests_total": ("counter", "HTTP requests handled, by method, route and status."),
    "orchestrator_http_request_seconds": ("histogram", "HTTP request latency in seconds."),
    "orchestrator_runs_created_total": ("counter", "Workflow runs created, by trigger."),
    "orchestrator_runs_settled_total": ("counter", "Workflow runs reaching a terminal status."),
    "orchestrator_run_duration_seconds": ("histogram", "Wall-clock duration of settled runs."),
    "orchestrator_steps_settled_total": ("counter", "Step runs reaching a terminal status."),
    "orchestrator_step_duration_seconds": ("histogram", "Wall-clock duration of settled steps."),
    "orchestrator_step_retries_total": ("counter", "Step attempts scheduled for retry."),
    "orchestrator_step_timeouts_total": ("counter", "Step attempts that exceeded their deadline."),
    "orchestrator_worker_claims_total": ("counter", "Tasks handed to workers."),
    "orchestrator_worker_heartbeats_total": ("counter", "Heartbeats accepted from workers."),
    "orchestrator_schedule_fires_total": ("counter", "Schedule occurrences that produced a run."),
    "orchestrator_outbox_published_total": ("counter", "Outbox messages relayed to the queue."),
    "orchestrator_outbox_failures_total": ("counter", "Outbox relay failures."),
    "orchestrator_uptime_seconds": ("gauge", "Seconds since the process started."),
    "orchestrator_runs_inflight": ("gauge", "Runs currently queued, running or cancelling."),
    "orchestrator_steps_inflight": ("gauge", "Steps currently pending, running or retrying."),
    "orchestrator_workers_active": ("gauge", "Workers seen within the staleness window."),
    "orchestrator_outbox_backlog": ("gauge", "Unpublished outbox messages."),
    "orchestrator_schedules_due": ("gauge", "Enabled schedules whose next run time has passed."),
    "orchestrator_dispatch_backend": ("gauge", "1 when Redis outbox dispatch is configured, else 0."),
    "orchestrator_queue_depth": ("gauge", "Steps awaiting a worker, by queue."),
    "orchestrator_claim_seconds": ("histogram", "Worker claim latency in seconds, by queue."),
    "orchestrator_lease_expirations_total": ("counter", "Step leases reclaimed after expiry."),
    "orchestrator_dlq_size": ("gauge", "Failed steps awaiting redrive (dead-letter queue)."),
    "orchestrator_tenant_runs_created_total": ("counter", "Workflow runs created, by owner."),
    "orchestrator_notification_deliveries_total": ("counter", "Notification deliveries, by channel type, event and status."),
}

_LABEL_ORDER = {
    "orchestrator_http_requests_total": ("method", "route", "status"),
    "orchestrator_http_request_seconds": ("method", "route"),
    "orchestrator_runs_created_total": ("trigger",),
    "orchestrator_runs_settled_total": ("status",),
    "orchestrator_steps_settled_total": ("status",),
    "orchestrator_step_retries_total": ("task_type",),
    "orchestrator_step_timeouts_total": ("task_type",),
    "orchestrator_worker_claims_total": ("worker_id",),
    "orchestrator_worker_heartbeats_total": (),
    "orchestrator_schedule_fires_total": (),
    "orchestrator_outbox_published_total": ("topic",),
    "orchestrator_outbox_failures_total": ("topic",),
    "orchestrator_queue_depth": ("queue",),
    "orchestrator_claim_seconds": ("queue",),
    "orchestrator_step_duration_seconds": ("task_type",),
    "orchestrator_tenant_runs_created_total": ("owner_id",),
    "orchestrator_notification_deliveries_total": ("channel_type", "event", "status"),
}


def _key(name: str, labels: dict[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
    return name, tuple(sorted(labels.items()))


def counter(name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
    key = _key(name, labels or {})
    with _LOCK:
        _COUNTERS[key] += value


def gauge(name: str, value: float, labels: dict[str, str] | None = None) -> None:
    with _LOCK:
        _GAUGES[_key(name, labels or {})] = value


def observe(name: str, seconds: float, labels: dict[str, str] | None = None) -> None:
    key = _key(name, labels or {})
    with _LOCK:
        bucket = _HISTOGRAMS[key]
        bucket.append(seconds)
        if len(bucket) > 4096:
            del bucket[: len(bucket) - 4096]


class timer:
    """Context manager that records elapsed seconds into a histogram."""

    def __init__(self, name: str, labels: dict[str, str] | None = None) -> None:
        self.name = name
        self.labels = labels or {}
        self.started = 0.0

    def __enter__(self) -> "timer":
        self.started = time.perf_counter()
        return self

    def __exit__(self, *_exc) -> None:
        observe(self.name, time.perf_counter() - self.started, self.labels)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(name: str, labels: tuple[tuple[str, str], ...]) -> str:
    order = _LABEL_ORDER.get(name, ())
    if not labels:
        return ""
    lookup = dict(labels)
    ordered: Iterable[tuple[str, str]] = [(key, lookup[key]) for key in order if key in lookup]
    extra = [(key, value) for key, value in labels if key not in order]
    pairs = list(ordered) + sorted(extra)
    rendered = ",".join(f'{key}="{_escape(str(value))}"' for key, value in pairs)
    return "{" + rendered + "}"


def render_prometheus() -> str:
    with _LOCK:
        counters = dict(_COUNTERS)
        gauges = dict(_GAUGES)
        histograms = {key: list(value) for key, value in _HISTOGRAMS.items()}
    lines: list[str] = []
    seen: set[str] = set()

    def header(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        kind, help_text = _HELP.get(name, ("untyped", name))
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")

    for (name, labels), value in sorted(counters.items()):
        header(name)
        lines.append(f"{name}{_format_labels(name, labels)} {_format_number(value)}")

    for (name, labels), samples in sorted(histograms.items()):
        header(name)
        buckets = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0)
        total = len(samples)
        for bound in buckets:
            count = sum(1 for sample in samples if sample <= bound)
            lines.append(f"{name}_bucket{_format_labels(name, labels + (('le', _format_number(bound)),))} {count}")
        lines.append(f"{name}_bucket{_format_labels(name, labels + (('le', '+Inf'),))} {total}")
        lines.append(f"{name}_sum{_format_labels(name, labels)} {_format_number(sum(samples))}")
        lines.append(f"{name}_count{_format_labels(name, labels)} {total}")

    for (name, labels), value in sorted(gauges.items()):
        header(name)
        lines.append(f"{name}{_format_labels(name, labels)} {_format_number(value)}")

    uptime_name = "orchestrator_uptime_seconds"
    header(uptime_name)
    lines.append(f"{uptime_name} {_format_number(time.time() - _STARTED_AT)}")
    return "\n".join(lines) + "\n"


def _format_number(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(round(value, 6))


def snapshot() -> dict[str, float]:
    """Flat snapshot used by the ops dashboard."""
    with _LOCK:
        result: dict[str, float] = {}
        for (name, labels), value in _COUNTERS.items():
            suffix = "|".join(f"{key}={val}" for key, val in labels) if labels else ""
            result[f"{name}|{suffix}" if suffix else name] = value
        for (name, labels), value in _GAUGES.items():
            suffix = "|".join(f"{key}={val}" for key, val in labels) if labels else ""
            result[f"{name}|{suffix}" if suffix else name] = value
        return result


def reset() -> None:
    """Test helper."""
    with _LOCK:
        _COUNTERS.clear()
        _GAUGES.clear()
        _HISTOGRAMS.clear()


__all__ = ["counter", "gauge", "observe", "render_prometheus", "reset", "snapshot", "timer"]
