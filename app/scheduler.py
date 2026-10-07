"""Standalone scheduler process.

    python -m app.scheduler

The API also runs a scheduler thread for single-process local development. For
multiple API replicas, disable the in-process loop (``SCHEDULER_ENABLED=false``)
and run one or more of these instead: each tick takes a PostgreSQL advisory lock,
so replicas never fire the same occurrence twice, and the per-slot idempotency
key is a second line of defence at the database level.
"""
from __future__ import annotations

import signal
import sys
import threading
import time

from app.config import settings
from app.core.logging import configure_logging, get_logger
from app.database import check_database, session_scope
from app.services import scheduler_service

logger = get_logger("app.scheduler")


def main() -> int:
    configure_logging()
    ok, error = check_database()
    if not ok:
        logger.error("Scheduler cannot start: database is not reachable", extra={"error": error})
        return 1

    stop = threading.Event()

    def handle(signum: int, _frame: object) -> None:
        logger.info("Scheduler shutdown signal received", extra={"signal": signum})
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is not None:
            try:
                signal.signal(signum, handle)
            except (ValueError, OSError):  # pragma: no cover - not the main thread
                pass

    logger.info("Scheduler process started", extra={"interval_seconds": settings.scheduler_interval_seconds})
    while not stop.is_set():
        try:
            with session_scope() as db:
                started = scheduler_service.tick(db)
                scheduler_service.scheduler_metrics(db)
            if started:
                logger.info("Started scheduled runs", extra={"count": started})
        except Exception as exc:  # pragma: no cover - resilience path
            logger.exception("Scheduler tick failed", extra={"error": str(exc)})
        stop.wait(settings.scheduler_interval_seconds)
    logger.info("Scheduler process stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
