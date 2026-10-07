"""In-memory sliding-window rate limiter.

Single-instance only; a multi-worker deployment should replace this with a
Redis-backed implementation. Limits are per key (IP address or user id) over
a rolling window.
"""
from __future__ import annotations

import time
from collections import deque
from threading import Lock


class RateLimiter:
    def __init__(self, *, max_hits: int, window_seconds: int):
        self.max_hits = max_hits
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds)."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = self._hits[key] = deque()
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.max_hits:
                retry_after = int(hits[0] + self.window_seconds - now) + 1
                return False, max(retry_after, 1)
            hits.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# Pre-configured limiters. Bounds come from settings at call time via the
# ``limited`` dependency so tests can monkeypatch them.
_auth_limiter = RateLimiter(max_hits=30, window_seconds=60)
_trigger_limiter = RateLimiter(max_hits=60, window_seconds=60)
_api_limiter = RateLimiter(max_hits=600, window_seconds=60)


def auth_limiter() -> RateLimiter:
    return _auth_limiter


def trigger_limiter() -> RateLimiter:
    return _trigger_limiter


def api_limiter() -> RateLimiter:
    """General per-IP API rate limiter (production: rate limiting)."""
    return _api_limiter


__all__ = ["RateLimiter", "auth_limiter", "trigger_limiter", "api_limiter"]
