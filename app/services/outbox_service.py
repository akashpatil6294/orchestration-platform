"""Transactional outbox for worker dispatch.

Why: a state change that makes a step claimable (``settle_run``) and the queue
message announcing it must not drift apart. Writing both in one database
transaction gives exactly that — if the transaction rolls back, no message
exists; if it commits, the message is durable in ``outbox_messages``.

Without ``REDIS_URL`` the table is a durable dispatch journal that workers
simply poll past (the database is the source of truth either way). With
``REDIS_URL`` set, :class:`OutboxRelay` publishes committed rows to a Redis
stream and marks them published, so Redis is a latency optimisation rather than
a second source of truth. Messages are published at least once; consumers must
be idempotent, which is why every task carries an idempotency key.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.core.logging import get_logger
from app.core.metrics import counter, gauge
from app.database import session_scope
from app.models.run import OutboxMessage

logger = get_logger("app.outbox")

TOPIC_TASK_READY = "task.ready"
TOPIC_TASK_CANCELLED = "task.cancelled"
TOPIC_RUN_SETTLED = "run.settled"


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def enqueue(db: Session, topic: str, payload: dict[str, Any]) -> OutboxMessage:
    """Append a message to the outbox. Commits with the caller's transaction."""
    message = OutboxMessage(topic=topic, payload=payload)
    db.add(message)
    return message


def pending_count(db: Session) -> int:
    from sqlalchemy import func

    return int(
        db.scalar(select(func.count()).select_from(OutboxMessage).where(OutboxMessage.published_at.is_(None))) or 0
    )


def claim_batch(db: Session, limit: int) -> list[OutboxMessage]:
    """Oldest unpublished messages, skipping ones that exceeded the retry cap."""
    return list(
        db.scalars(
            select(OutboxMessage)
            .where(OutboxMessage.published_at.is_(None), OutboxMessage.attempts < settings.outbox_max_attempts)
            .order_by(OutboxMessage.created_at)
            .limit(limit)
        ).all()
    )


def mark_published(db: Session, messages: list[OutboxMessage]) -> None:
    now = utcnow()
    for message in messages:
        message.published_at = now
        message.last_error = None


def mark_failed(db: Session, message: OutboxMessage, error: str) -> None:
    message.attempts += 1
    message.last_error = error[:500]


def prune_published(db: Session, *, older_than_hours: int = 24) -> int:
    """Housekeeping: drop published messages older than the retention window."""
    cutoff = utcnow() - timedelta(hours=older_than_hours)
    rows = list(
        db.scalars(select(OutboxMessage).where(OutboxMessage.published_at.is_not(None), OutboxMessage.published_at < cutoff)).all()
    )
    for row in rows:
        db.delete(row)
    return len(rows)


class OutboxRelay:
    """Publishes committed outbox rows to Redis, when Redis is configured.

    Runs as a daemon thread inside the API process. Failures are recorded on the
    row and retried; nothing is ever dropped silently.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self._connected = False

    # ------------------------------------------------------------------ redis
    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        import redis  # imported lazily so Redis stays optional

        self._client = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=5, health_check_interval=30)
        self._client.ping()
        self._connected = True
        return self._client

    def _publish(self, messages: list[OutboxMessage]) -> int:
        client = self._connect()
        published = 0
        with session_scope() as db:
            for message in messages:
                fresh = db.get(OutboxMessage, message.id)
                if fresh is None or fresh.published_at is not None:
                    continue
                try:
                    client.xadd(
                        settings.outbox_stream,
                        {
                            "id": fresh.id,
                            "topic": fresh.topic,
                            "payload": __import__("json").dumps(fresh.payload),
                            "created_at": fresh.created_at.isoformat(),
                        },
                        maxlen=100_000,
                        approximate=True,
                    )
                    fresh.published_at = utcnow()
                    fresh.last_error = None
                    counter("orchestrator_outbox_published_total", {"topic": fresh.topic})
                    published += 1
                except Exception as exc:  # pragma: no cover - depends on Redis
                    mark_failed(db, fresh, f"{type(exc).__name__}: {exc}")
                    counter("orchestrator_outbox_failures_total", {"topic": fresh.topic})
                    logger.warning("Outbox publish failed", extra={"outbox_id": fresh.id, "error": str(exc)})
        return published

    # ----------------------------------------------------------------- thread
    def _loop(self) -> None:
        logger.info("Outbox relay started", extra={"stream": settings.outbox_stream})
        while not self._stop.wait(settings.outbox_relay_interval_seconds):
            try:
                with session_scope() as db:
                    gauge("orchestrator_outbox_backlog", float(pending_count(db)))
                    batch = claim_batch(db, settings.dispatch_batch_size)
                if batch:
                    self._publish(batch)
            except Exception as exc:  # pragma: no cover - resilience path
                self._connected = False
                self._client = None
                logger.warning("Outbox relay cycle failed", extra={"error": str(exc)})
                self._stop.wait(min(30.0, settings.outbox_relay_interval_seconds * 5))
        logger.info("Outbox relay stopped")

    def start(self) -> None:
        if not settings.redis_url:
            logger.info("Redis dispatch disabled; workers poll the database directly")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="outbox-relay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


relay = OutboxRelay()

__all__ = [
    "TOPIC_RUN_SETTLED",
    "TOPIC_TASK_CANCELLED",
    "TOPIC_TASK_READY",
    "OutboxRelay",
    "claim_batch",
    "enqueue",
    "mark_failed",
    "mark_published",
    "pending_count",
    "prune_published",
    "relay",
]
