"""Sample worker entry point.

    python -m app.sample_worker

Environment (see ``.env.example``):

* ``ORCHESTRATOR_API``  base URL of the API (default ``http://127.0.0.1:8000``)
* ``WORKER_TOKEN``      credential minted with
                        ``python -m app.cli create-worker --worker-id sample-worker-1``
* ``WORKER_ID``         worker identity (default ``sample-worker-1``)
* ``WORKER_MAX_CONCURRENCY`` how many tasks this worker runs at once
* ``WORKER_TASK_TYPES`` optional comma-separated subset of the handler allow-list
* ``WORKER_HEARTBEAT_INTERVAL`` seconds between task lease renewals (default 10)

The set of task types this worker will ever execute is the registry in
``app/worker/handlers.py`` — nothing in a workflow definition can add to it.
"""
from __future__ import annotations

import sys

from app.config import settings
from app.core.logging import configure_logging, get_logger
from app.worker import handlers as _handlers  # noqa: F401  (registers the demo handlers)
from app.worker.registry import registry
from app.worker.runtime import ApiError, Worker

logger = get_logger("app.sample_worker")


def main() -> int:
    configure_logging()
    if not settings.worker_token:
        logger.error(
            "WORKER_TOKEN is not set. Mint one with:  python -m app.cli create-worker --worker-id "
            f"{settings.worker_id}"
        )
        return 2
    worker = Worker()
    print(f"Worker {worker.worker_id} will execute: {', '.join(registry.types())}")
    print(f"Polling {worker.api.base_url} (Ctrl+C to stop)")
    try:
        worker.run()
    except ApiError as exc:
        logger.error("Worker could not start", extra={"status": exc.status, "error": exc.message})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
