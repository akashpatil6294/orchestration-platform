"""Run quotas: per-user daily and concurrent limits with 429 responses.

Limits come from settings (``quota_runs_per_day``, ``quota_concurrent_runs``);
a value of 0 disables that check. When a quota is exceeded the caller gets a
``RateLimited`` error whose details carry ``limit``, ``usage`` and ``reset``
(the ISO timestamp when the window rolls over).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.errors import RateLimited
from app.models.run import WorkflowRun as Run
from app.services.run_service import ACTIVE_RUN_STATUSES


def check_run_quota(db: Session, *, triggered_by: str) -> None:
    """Raise ``RateLimited`` when the user is over a run quota.

    ``triggered_by`` is the actor label stored on runs (user email for
    interactive starts). A limit of 0 disables that check.
    """
    now = datetime.now(timezone.utc)

    daily_limit = settings.quota_runs_per_day
    if daily_limit > 0:
        window_start = now - timedelta(days=1)
        usage = (
            db.scalar(
                select(func.count())
                .select_from(Run)
                .where(Run.triggered_by == triggered_by, Run.created_at >= window_start)
            )
            or 0
        )
        if usage >= daily_limit:
            raise RateLimited(
                f"Daily run quota exceeded ({usage}/{daily_limit})",
                code="quota_exceeded",
                details={
                    "quota": "runs_per_day",
                    "limit": daily_limit,
                    "usage": usage,
                    "reset": (window_start + timedelta(days=1)).isoformat(),
                },
            )

    concurrent_limit = settings.quota_concurrent_runs
    if concurrent_limit > 0:
        usage = (
            db.scalar(
                select(func.count())
                .select_from(Run)
                .where(Run.triggered_by == triggered_by, Run.status.in_(ACTIVE_RUN_STATUSES))
            )
            or 0
        )
        if usage >= concurrent_limit:
            raise RateLimited(
                f"Concurrent run quota exceeded ({usage}/{concurrent_limit})",
                code="quota_exceeded",
                details={
                    "quota": "concurrent_runs",
                    "limit": concurrent_limit,
                    "usage": usage,
                    "reset": None,
                },
            )


def quota_status(db: Session, *, triggered_by: str) -> dict:
    """Current usage vs limits for the quota meters UI."""
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=1)

    daily_limit = settings.quota_runs_per_day
    daily_usage = (
        db.scalar(
            select(func.count())
            .select_from(Run)
            .where(Run.triggered_by == triggered_by, Run.created_at >= window_start)
        )
        or 0
    )

    concurrent_limit = settings.quota_concurrent_runs
    concurrent_usage = (
        db.scalar(
            select(func.count())
            .select_from(Run)
            .where(Run.triggered_by == triggered_by, Run.status.in_(ACTIVE_RUN_STATUSES))
        )
        or 0
    )

    return {
        "runs_per_day": {"limit": daily_limit, "usage": daily_usage},
        "concurrent_runs": {"limit": concurrent_limit, "usage": concurrent_usage},
    }


__all__ = ["check_run_quota", "quota_status"]
