"""Chaos injection for reliability testing (Stage H, H5).

Controlled via environment variables so chaos can be enabled per worker
process without code changes:

* ``CHAOS_FAILURE_RATE`` (0.0-1.0): probability a task fails with a retryable error
* ``CHAOS_SLOW_SECONDS``: artificial delay before each task (simulates slow handlers)
* ``CHAOS_FAIL_STEPS``: comma-separated step keys that always fail (retryable)
* ``CHAOS_DUPLICATE_RATE`` (0.0-1.0): probability the worker reports success twice
  (exercises idempotency handling on the server)

All chaos is opt-in and defaults to off. Never enable in production.
"""
from __future__ import annotations

import os
import random
import time


class ChaosConfig:
    def __init__(self) -> None:
        self.failure_rate = _float("CHAOS_FAILURE_RATE", 0.0)
        self.slow_seconds = _float("CHAOS_SLOW_SECONDS", 0.0)
        self.fail_steps = {s.strip() for s in os.environ.get("CHAOS_FAIL_STEPS", "").split(",") if s.strip()}
        self.duplicate_rate = _float("CHAOS_DUPLICATE_RATE", 0.0)

    @property
    def enabled(self) -> bool:
        return (
            self.failure_rate > 0
            or self.slow_seconds > 0
            or bool(self.fail_steps)
            or self.duplicate_rate > 0
        )

    def describe(self) -> str:
        parts = []
        if self.failure_rate > 0:
            parts.append(f"failure_rate={self.failure_rate}")
        if self.slow_seconds > 0:
            parts.append(f"slow_seconds={self.slow_seconds}")
        if self.fail_steps:
            parts.append(f"fail_steps={','.join(sorted(self.fail_steps))}")
        if self.duplicate_rate > 0:
            parts.append(f"duplicate_rate={self.duplicate_rate}")
        return "; ".join(parts) or "disabled"


def _float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


class ChaosError(RuntimeError):
    """Synthetic retryable failure injected by chaos mode."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"chaos injected failure: {reason}")
        self.reason = reason


def maybe_inject(chaos: ChaosConfig, step_key: str) -> None:
    """Apply pre-execution chaos: delays and failures. Raises ChaosError."""
    if not chaos.enabled:
        return
    if chaos.slow_seconds > 0:
        time.sleep(chaos.slow_seconds)
    if step_key in chaos.fail_steps:
        raise ChaosError(f"step {step_key} is in CHAOS_FAIL_STEPS")
    if chaos.failure_rate > 0 and random.random() < chaos.failure_rate:
        raise ChaosError(f"random failure (rate={chaos.failure_rate})")


def should_duplicate(chaos: ChaosConfig) -> bool:
    return chaos.enabled and chaos.duplicate_rate > 0 and random.random() < chaos.duplicate_rate
