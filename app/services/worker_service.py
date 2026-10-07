"""Worker registration, task claiming, leases, heartbeats and results.

Concurrency model
-----------------
A claim is a **conditional UPDATE** that only succeeds while the step is still
claimable. Two workers polling at the same moment therefore cannot both win: the
loser sees ``rowcount == 0`` and moves on. The winner receives a single-use
``lease_token`` that must accompany every subsequent heartbeat, completion or
failure report, and which is never exposed in a user-facing response.

Reliability model
-----------------
* Every claim carries a **lease** (renewed by heartbeat) and a **deadline**
  derived from the step timeout. Whichever expires first, the attempt is
  reclaimed and retried according to the step's retry policy.
* Reclaiming is itself conditional, so a worker that reports success at the same
  instant its lease expires cannot clobber the reclaimed attempt.
* A worker that reports an error may mark it non-retryable, which fails the step
  immediately instead of burning the remaining attempts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.config import settings
from app.core.dataflow import resolve_templates
from app.core.engine import join_decision
from app.core.errors import Conflict, Forbidden, NotFound
from app.core.logging import get_logger
from app.core.metrics import counter
from app.core.pagination import check_task_output
from app.core.security import hash_token, redact, token_prefix
from app.models.base import utc as ensure_utc
from app.models.event import EventType
from app.models.run import ConcurrencyGate, StepAttempt, StepRun, TaskRateBucket, WorkflowRun
from app.models.worker import Worker, WorkerHeartbeat
from app.models.workflow import Workflow
from app.services import event_service, run_service
from app.services.outbox_service import TOPIC_TASK_READY, enqueue

logger = get_logger("app.workers")

ACTIVE_STEP_STATUSES = {"pending", "running", "retrying"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def heartbeat_interval() -> int:
    return settings.worker_heartbeat_interval


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def register_worker(
    db: Session,
    *,
    worker_id: str,
    task_types: list[str],
    queues: list[str] | None = None,
    name: str = "",
    max_concurrency: int = 1,
    metadata: dict[str, Any] | None = None,
    token: str | None = None,
) -> Worker:
    worker = db.get(Worker, worker_id)
    if worker is None:
        worker = Worker(id=worker_id)
        db.add(worker)
    if token:
        # A re-registration with a fresh token rotates the credential.
        worker.token_hash = hash_token(token)
        worker.token_prefix = token_prefix(token)
    worker.name = name or worker.name or worker_id
    worker.task_types = sorted({item for item in task_types if item})
    worker.queues = sorted({item for item in (queues or ["default"]) if item}) or ["default"]
    worker.max_concurrency = max_concurrency
    worker.metadata_json = metadata or {}
    worker.active = True
    worker.last_seen_at = utcnow()
    db.flush()
    return worker


def worker_view(db: Session, worker: Worker) -> dict[str, Any]:
    running = int(
        db.scalar(select(func.count()).select_from(StepRun).where(StepRun.worker_id == worker.id, StepRun.status == "running")) or 0
    )
    return {
        "worker_id": worker.id,
        "name": worker.name,
        "active": worker.active,
        "stale": worker.is_stale,
        "task_types": list(worker.task_types or []),
        "queues": list(worker.queues or ["default"]),
        "max_concurrency": worker.max_concurrency,
        "last_seen_at": worker.last_seen_at,
        "created_at": worker.created_at,
        "running_steps": running,
    }


def list_workers(db: Session, owner_id: str | None = None) -> list[Worker]:
    query = select(Worker)
    if owner_id:
        query = query.where(or_(Worker.owner_id == owner_id, Worker.owner_id.is_(None)))
    return list(db.scalars(query.order_by(Worker.last_seen_at.desc())).all())


def set_worker_active(db: Session, worker: Worker, active: bool) -> int:
    """Activate/deactivate a worker, releasing anything it currently holds."""
    worker.active = active
    worker.last_seen_at = utcnow()
    released = 0
    if not active:
        released = release_worker_tasks(db, worker.id, reason="worker_shutdown")
    db.flush()
    return released


def release_worker_tasks(db: Session, worker_id: str, *, reason: str) -> int:
    """Return a worker's in-flight steps to the queue for another worker."""
    steps = list(db.scalars(select(StepRun).where(StepRun.worker_id == worker_id, StepRun.status == "running")).all())
    now = utcnow()
    released = 0
    for step in steps:
        run = db.get(WorkflowRun, step.run_id)
        if run and run.cancel_requested and not (
            ((step.spec_json or {}).get("failure_handler") or (step.spec_json or {}).get("compensation_only"))
            and run.error
            and run.error.get("code") == "run_timeout"
        ):
            status = "cancelled"
        else:
            status = "retrying" if step.attempts <= step.retry_limit else "failed"
        step.status = status
        step.lease_token = None
        step.lease_expires_at = None
        step.deadline_at = None
        step.worker_id = None
        step.error = {"code": reason, "message": "The worker stopped before reporting a result"}
        step.available_at = now + timedelta(seconds=run_service.retry_delay_seconds(step)) if status == "retrying" else now
        step.updated_at = now
        _close_attempt(db, step, "abandoned", error=step.error)
        db.flush()
        _refresh_concurrency_gates_for_step(db, step, run) if run else None
        event_service.emit(
            db, EventType.STEP_LEASE_EXPIRED, run_id=step.run_id, step=step, level="warning",
            message="Worker stopped holding this task; it will be retried",
            payload={"worker_id": worker_id, "reason": reason, "next_status": status},
        )
        if run:
            run_service.settle_run(db, run)
        released += 1
    return released


# --------------------------------------------------------------------------- #
# Claiming
# --------------------------------------------------------------------------- #
def claim_task(
    db: Session,
    *,
    worker: Worker,
    available_slots: int = 1,
    task_types: list[str] | None = None,
    queues: list[str] | None = None,
    known_task_ids: list[str] | None = None,
    record_heartbeat: bool = True,
) -> dict[str, Any] | None:
    """Claim ready steps and recover assignments whose claim response was lost."""
    from app.core.metrics import timer as _timer

    queue_label = ",".join(sorted(queues or worker.queues or ["default"]))
    with _timer("orchestrator_claim_seconds", {"queue": queue_label}):
        return _claim_task_inner(
            db,
            worker=worker,
            available_slots=available_slots,
            task_types=task_types,
            queues=queues,
            known_task_ids=known_task_ids,
            record_heartbeat=record_heartbeat,
        )


def _claim_task_inner(
    db: Session,
    *,
    worker: Worker,
    available_slots: int = 1,
    task_types: list[str] | None = None,
    queues: list[str] | None = None,
    known_task_ids: list[str] | None = None,
    record_heartbeat: bool = True,
) -> dict[str, Any] | None:
    worker.last_seen_at = utcnow()
    if record_heartbeat:
        db.add(WorkerHeartbeat(worker_id=worker.id, active_tasks=worker_running_count(db, worker.id), recorded_at=worker.last_seen_at))
    recover_expired_leases(db)

    slots = max(1, min(available_slots, worker.max_concurrency or 1))
    allowed = set(task_types if task_types is not None else (worker.task_types or []))
    allowed_queues = set(queues if queues is not None else (worker.queues or ["default"]))

    # A claim transaction may commit before its HTTP response reaches the
    # worker. On the next poll, return any still-leased assignments the worker
    # does not know about so they can be heartbeated and completed instead of
    # expiring unseen.
    if known_task_ids is not None:
        now_aware = ensure_utc(utcnow())
        recover_query = select(StepRun).where(
            StepRun.worker_id == worker.id,
            StepRun.status == "running",
            StepRun.lease_expires_at > now_aware,
            StepRun.deadline_at > now_aware,
        )
        if known_task_ids:
            recover_query = recover_query.where(StepRun.id.not_in(known_task_ids))
        held = list(db.scalars(recover_query.order_by(StepRun.started_at).limit(slots)).all())
        recovered: list[dict[str, Any]] = []
        for step in held:
            run = db.get(WorkflowRun, step.run_id)
            if run is not None:
                recovered.append(_assignment(db, step, run, step.lease_token or ""))
        if recovered:
            return {"tasks": recovered, "task": recovered[0]}

    claimed: list[dict[str, Any]] = []
    for _ in range(slots):
        assignment = _claim_one(db, worker, allowed, allowed_queues)
        if assignment is None:
            break
        # Flush between claims so the next candidate query sees this claim.
        # Autoflush is disabled, so without this the same step could be handed
        # out twice within a single request.
        db.flush()
        claimed.append(assignment)
    db.flush()
    if not claimed:
        return None
    return {"tasks": claimed, "task": claimed[0]}


def _claim_one(db: Session, worker: Worker, allowed: set[str], allowed_queues: set[str]) -> dict[str, Any] | None:
    now = utcnow()
    now_aware = ensure_utc(now)
    active_by_owner = (
        select(Workflow.owner_id.label("owner_id"), func.count(StepRun.id).label("active_count"))
        .select_from(StepRun)
        .join(WorkflowRun, WorkflowRun.id == StepRun.run_id)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(StepRun.status == "running")
        .group_by(Workflow.owner_id)
        .subquery()
    )
    candidate_query = (
        select(StepRun)
        .join(WorkflowRun, WorkflowRun.id == StepRun.run_id)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .outerjoin(active_by_owner, active_by_owner.c.owner_id == Workflow.owner_id)
        .where(StepRun.status.in_(("pending", "retrying")), StepRun.available_at <= now_aware)
        .order_by(
            func.coalesce(active_by_owner.c.active_count, 0),
            StepRun.priority.desc(),
            WorkflowRun.priority.desc(),
            StepRun.available_at,
            StepRun.created_at,
        )
        .limit(settings.dispatch_batch_size)
    )
    if allowed:
        candidate_query = candidate_query.where(StepRun.task_type.in_(allowed))
    if allowed_queues:
        candidate_query = candidate_query.where(StepRun.queue_name.in_(allowed_queues))
    if db.get_bind().dialect.name == "postgresql":
        candidate_query = candidate_query.with_for_update(skip_locked=True, of=StepRun)
    candidates = list(db.scalars(candidate_query).all())
    if not candidates:
        return None

    # Preload the runs and their step sets so dependency checks are cheap.
    # PostgreSQL also locks each run row so concurrent claims against the same
    # run cannot both admit work past the run's parallel budget.
    run_ids = {candidate.run_id for candidate in candidates}
    run_query = select(WorkflowRun).where(WorkflowRun.id.in_(run_ids))
    if db.get_bind().dialect.name == "postgresql":
        run_query = run_query.with_for_update()
    runs = {run.id: run for run in db.scalars(run_query).all()}
    workflows = {
        workflow.id: workflow
        for workflow in db.scalars(select(Workflow).where(Workflow.id.in_({run.workflow_id for run in runs.values()}))).all()
    }
    steps_by_run: dict[str, dict[str, StepRun]] = {}
    for step in db.scalars(select(StepRun).where(StepRun.run_id.in_(run_ids))).all():
        steps_by_run.setdefault(step.run_id, {})[step.step_key] = step

    # Respect each run's concurrency budget across all workers.
    running_by_run: dict[str, int] = {}
    for run_id, mapping in steps_by_run.items():
        running_by_run[run_id] = sum(1 for step in mapping.values() if step.status == "running")

    for candidate in candidates:
        run = runs.get(candidate.run_id)
        handler_after_timeout = bool(
            ((candidate.spec_json or {}).get("failure_handler") or (candidate.spec_json or {}).get("compensation_only"))
            and run is not None
            and run.error
            and run.error.get("code") == "run_timeout"
        )
        if run is None or (run.cancel_requested and not handler_after_timeout) or run.status == "paused":
            continue
        if allowed and candidate.task_type not in allowed:
            continue
        if allowed_queues and candidate.queue_name not in allowed_queues:
            continue
        siblings = steps_by_run.get(candidate.run_id, {})
        dependencies = [siblings[dep] for dep in (candidate.depends_on or []) if siblings.get(dep) is not None]
        if join_decision(dependencies, mode=(candidate.spec_json or {}).get("join", "all_success")) != "ready":
            continue
        budget = max(1, run.max_parallel or 1)
        if running_by_run.get(candidate.run_id, 0) >= budget:
            continue
        if candidate.parent_step_id:
            fanout_limit = (candidate.spec_json or {}).get("max_concurrency")
            if fanout_limit:
                active_siblings = sum(
                    1 for item in siblings.values()
                    if item.parent_step_id == candidate.parent_step_id and item.status == "running"
                )
                if active_siblings >= int(fanout_limit):
                    continue

        workflow = workflows.get(run.workflow_id)
        if workflow is None:
            continue
        if _circuit_breaker_open(db, workflow_owner_id=workflow.owner_id, step=candidate, now=now):
            continue
        gates = _reserve_concurrency_gates(db, workflow.owner_id, candidate)
        if gates is None:
            continue
        rate_bucket = _consume_rate_token(db, workflow.owner_id, candidate, now)
        if rate_bucket is False:
            _refresh_reserved_gates(db, gates)
            continue

        token = _new_lease_token()
        claimed_at = now
        deadline = claimed_at + timedelta(seconds=candidate.timeout_seconds)
        lease_until = min(claimed_at + timedelta(seconds=settings.lease_seconds), deadline)
        result = db.execute(
            update(StepRun)
            .where(
                StepRun.id == candidate.id,
                StepRun.status == candidate.status,
                StepRun.available_at <= now_aware,
            )
            .execution_options(synchronize_session=False)
            .values(
                status="running",
                attempts=StepRun.attempts + 1,
                worker_id=worker.id,
                lease_token=token,
                lease_expires_at=lease_until,
                deadline_at=deadline,
                started_at=candidate.started_at or claimed_at,
                updated_at=claimed_at,
            )
        )
        if result.rowcount != 1:
            # Another worker won the race; try the next candidate.
            if rate_bucket:
                _refund_rate_token(db, rate_bucket, _rate_limit_for(candidate))
            _refresh_reserved_gates(db, gates)
            db.expire(candidate)
            continue

        # The budget check above read a snapshot; a concurrent claim can commit
        # in between, so verify the run's parallelism once the row is ours and
        # hand the claim back when the run is already full.
        admitted_running = int(
            db.scalar(
                select(func.count())
                .select_from(StepRun)
                .where(StepRun.run_id == candidate.run_id, StepRun.status == "running")
            )
            or 0
        )
        if admitted_running > budget:
            db.execute(
                update(StepRun)
                .where(StepRun.id == candidate.id, StepRun.lease_token == token)
                .execution_options(synchronize_session=False)
                .values(
                    status=candidate.status,
                    attempts=StepRun.attempts - 1,
                    worker_id=None,
                    lease_token=None,
                    lease_expires_at=None,
                    deadline_at=None,
                    started_at=candidate.started_at,
                    finished_at=candidate.finished_at,
                    updated_at=utcnow(),
                )
            )
            if rate_bucket:
                _refund_rate_token(db, rate_bucket, _rate_limit_for(candidate))
            _refresh_reserved_gates(db, gates)
            db.expire(candidate)
            continue

        candidate.attempts += 1
        candidate.status = "running"
        candidate.worker_id = worker.id
        candidate.lease_token = token
        candidate.lease_expires_at = lease_until
        candidate.deadline_at = deadline
        candidate.started_at = candidate.started_at or claimed_at
        candidate.updated_at = claimed_at
        running_by_run[candidate.run_id] = running_by_run.get(candidate.run_id, 0) + 1

        db.add(StepAttempt(step_run_id=candidate.id, attempt_no=candidate.attempts, worker_id=worker.id, status="running"))
        if run.status in {"queued", "cancelling"} and not run.cancel_requested:
            run.status = "running"
            run.started_at = run.started_at or claimed_at
            run.updated_at = claimed_at
        event_service.emit(
            db, EventType.STEP_CLAIMED, run_id=run.id, step=candidate,
            message=f"Claimed by {worker.name or worker.id} (attempt {candidate.attempts})",
            payload={"worker_id": worker.id, "attempt": candidate.attempts, "lease_seconds": settings.lease_seconds},
        )
        counter("orchestrator_worker_claims_total", {"worker_id": worker.id})
        return _assignment(db, candidate, run, token)

    return None


def _gate_key(scope: str, owner_id: str | None = None, value: str | None = None) -> str:
    raw = f"{scope}\0{owner_id or ''}\0{value or ''}"
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _active_count_for_gate(db: Session, gate: ConcurrencyGate) -> int:
    query = select(func.count()).select_from(StepRun).where(StepRun.status == "running")
    if gate.scope == "queue":
        query = query.where(StepRun.queue_name == gate.queue_name)
    elif gate.scope in {"owner", "resource"}:
        query = query.join(WorkflowRun, WorkflowRun.id == StepRun.run_id).join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        query = query.where(Workflow.owner_id == gate.owner_id)
        if gate.scope == "resource":
            query = query.where(StepRun.concurrency_key == gate.resource_key)
    return int(db.scalar(query) or 0)


def _upsert_gate(db: Session, *, scope: str, owner_id: str | None, queue_name: str | None, resource_key: str | None, capacity: int) -> ConcurrencyGate:
    value = queue_name if scope == "queue" else resource_key if scope == "resource" else owner_id
    key = _gate_key(scope, owner_id if scope != "global" else None, value)
    values = {
        "key": key,
        "scope": scope,
        "owner_id": owner_id,
        "queue_name": queue_name,
        "resource_key": resource_key,
        "active_count": 0,
        "capacity": capacity,
        "updated_at": utcnow(),
    }
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        db.execute(pg_insert(ConcurrencyGate).values(**values).on_conflict_do_nothing(index_elements=[ConcurrencyGate.key]))
    elif dialect == "sqlite":
        db.execute(sqlite_insert(ConcurrencyGate).values(**values).on_conflict_do_nothing(index_elements=[ConcurrencyGate.key]))
    else:
        existing = db.get(ConcurrencyGate, key)
        if existing is None:
            db.add(ConcurrencyGate(**values))
            db.flush()
    query = select(ConcurrencyGate).where(ConcurrencyGate.key == key)
    if dialect == "postgresql":
        query = query.with_for_update()
    gate = db.scalar(query)
    if gate is None:
        raise RuntimeError("Could not initialise a concurrency gate")
    gate.capacity = capacity
    gate.active_count = _active_count_for_gate(db, gate)
    gate.updated_at = utcnow()
    return gate


def _reserve_concurrency_gates(db: Session, owner_id: str, step: StepRun) -> list[ConcurrencyGate] | None:
    limits: list[tuple[str, str | None, str | None, str | None, int]] = []
    if settings.max_global_running_tasks > 0:
        limits.append(("global", None, None, None, settings.max_global_running_tasks))
    queue_limit = int(settings.queue_concurrency_limits.get(step.queue_name, 0) or 0)
    if queue_limit > 0:
        limits.append(("queue", None, step.queue_name, None, queue_limit))
    if settings.max_running_tasks_per_owner > 0:
        limits.append(("owner", owner_id, None, None, settings.max_running_tasks_per_owner))
    if step.concurrency_key and step.concurrency_limit:
        limits.append(("resource", owner_id, None, step.concurrency_key, step.concurrency_limit))
    gates: list[ConcurrencyGate] = []
    for scope, owner, queue, resource, capacity in sorted(limits, key=lambda item: _gate_key(item[0], item[1], item[2] or item[3])):
        gate = _upsert_gate(
            db,
            scope=scope,
            owner_id=owner,
            queue_name=queue,
            resource_key=resource,
            capacity=capacity,
        )
        if gate.active_count >= capacity:
            _refresh_reserved_gates(db, gates)
            return None
        gate.active_count += 1
        gate.updated_at = utcnow()
        gates.append(gate)
    return gates


def _refresh_reserved_gates(db: Session, gates: list[ConcurrencyGate]) -> None:
    for gate in gates:
        gate.active_count = _active_count_for_gate(db, gate)
        gate.updated_at = utcnow()


def _consume_rate_token(db: Session, owner_id: str, step: StepRun, now: datetime) -> str | None | bool:
    policy = step.spec_json or {}
    per_minute = _rate_limit_for(step)
    if per_minute <= 0:
        return None
    bucket_name = str(policy.get("rate_limit_key") or step.task_type)
    import hashlib

    key = hashlib.sha256(f"{owner_id}\0{bucket_name}".encode("utf-8")).hexdigest()
    values = {"key": key, "tokens": float(per_minute), "updated_at": now}
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        db.execute(pg_insert(TaskRateBucket).values(**values).on_conflict_do_nothing(index_elements=[TaskRateBucket.key]))
    elif dialect == "sqlite":
        db.execute(sqlite_insert(TaskRateBucket).values(**values).on_conflict_do_nothing(index_elements=[TaskRateBucket.key]))
    else:
        existing = db.get(TaskRateBucket, key)
        if existing is None:
            db.add(TaskRateBucket(**values))
            db.flush()
    query = select(TaskRateBucket).where(TaskRateBucket.key == key)
    if dialect == "postgresql":
        query = query.with_for_update()
    bucket = db.scalar(query)
    if bucket is None:
        raise RuntimeError("Could not initialise a task rate bucket")
    elapsed = max(0.0, (now - bucket.updated_at).total_seconds())
    bucket.tokens = min(float(per_minute), bucket.tokens + elapsed * per_minute / 60.0)
    bucket.updated_at = now
    if bucket.tokens < 1.0:
        return False
    bucket.tokens -= 1.0
    return key


def _rate_limit_for(step: StepRun) -> int:
    policy = step.spec_json or {}
    return int(policy.get("rate_limit_per_minute") or settings.task_rate_limits.get(step.task_type, 0) or 0)


def _refund_rate_token(db: Session, bucket_key: str, per_minute: int) -> None:
    bucket = db.get(TaskRateBucket, bucket_key)
    if bucket is not None:
        bucket.tokens = min(float(max(1, per_minute)), bucket.tokens + 1.0)
        bucket.updated_at = utcnow()


def _circuit_breaker_open(db: Session, *, workflow_owner_id: str, step: StepRun, now: datetime) -> bool:
    threshold = settings.circuit_breaker_failure_threshold
    if threshold <= 0:
        return False
    base_query = (
        select(StepAttempt)
        .join(StepRun, StepRun.id == StepAttempt.step_run_id)
        .join(WorkflowRun, WorkflowRun.id == StepRun.run_id)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(Workflow.owner_id == workflow_owner_id, StepRun.task_type == step.task_type)
    )
    last_success = db.scalar(
        select(func.max(StepAttempt.finished_at))
        .select_from(StepAttempt)
        .join(StepRun, StepRun.id == StepAttempt.step_run_id)
        .join(WorkflowRun, WorkflowRun.id == StepRun.run_id)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(
            Workflow.owner_id == workflow_owner_id,
            StepRun.task_type == step.task_type,
            StepAttempt.status == "succeeded",
        )
    )
    retryable_failed = (StepAttempt.status == "failed") & (StepAttempt.error["retryable"].as_boolean().is_(True))
    failure_filter = StepAttempt.status.in_(("retrying", "lease_expired", "timed_out")) | retryable_failed
    # Build the count from a fresh base query so owner/type predicates and the
    # success watermark remain explicit across SQLite and PostgreSQL.
    failed_attempts = base_query.where(failure_filter)
    if last_success is not None:
        failed_attempts = failed_attempts.where(StepAttempt.started_at > last_success)
    failure_count = int(db.scalar(select(func.count()).select_from(failed_attempts.subquery())) or 0)
    if failure_count < threshold:
        return False
    latest_failure = db.scalar(
        select(func.max(StepAttempt.finished_at))
        .select_from(StepAttempt)
        .join(StepRun, StepRun.id == StepAttempt.step_run_id)
        .join(WorkflowRun, WorkflowRun.id == StepRun.run_id)
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .where(Workflow.owner_id == workflow_owner_id, StepRun.task_type == step.task_type, failure_filter)
    )
    return bool(latest_failure and (now - latest_failure.replace(tzinfo=None)).total_seconds() < settings.circuit_breaker_open_seconds)


def _refresh_concurrency_gates_for_step(db: Session, step: StepRun, run: WorkflowRun) -> None:
    db.flush()
    workflow = db.get(Workflow, run.workflow_id)
    if workflow is None:
        return
    relevant = or_(
        ConcurrencyGate.scope == "global",
        (ConcurrencyGate.scope == "queue") & (ConcurrencyGate.queue_name == step.queue_name),
        (ConcurrencyGate.scope == "owner") & (ConcurrencyGate.owner_id == workflow.owner_id),
        (ConcurrencyGate.scope == "resource")
        & (ConcurrencyGate.owner_id == workflow.owner_id)
        & (ConcurrencyGate.resource_key == step.concurrency_key),
    )
    query = select(ConcurrencyGate).where(relevant).order_by(ConcurrencyGate.key)
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    for gate in db.scalars(query).all():
        gate.active_count = _active_count_for_gate(db, gate)
        gate.updated_at = utcnow()


def _new_lease_token() -> str:
    import secrets

    return secrets.token_urlsafe(32)


def _assignment(db: Session, step: StepRun, run: WorkflowRun, token: str) -> dict[str, Any]:
    siblings = {row.step_key: row for row in run_service.steps_for(db, run.id)}
    dependency_outputs = {
        key: siblings[key].output_data for key in (step.depends_on or []) if key in siblings and siblings[key].status == "succeeded"
    }
    from app.services.connection_service import resolve_connections
    from app.services.workflow_service import resolve_secrets

    template_resolved_input = resolve_templates(
        step.input_data or {},
        workflow_input=run_service.workflow_input_for_run(run),
        step_outputs=dependency_outputs,
    )
    resolved_input, redacted_keys = resolve_secrets(db, run.workflow_id, template_resolved_input)
    resolved_input, connection_paths = resolve_connections(db, run.workflow_id, resolved_input)
    redacted_keys = list(redacted_keys) + list(connection_paths)
    return {
        "id": step.id,
        "run_id": run.id,
        "workflow_id": run.workflow_id,
        "workflow_version": run.version,
        "step_key": step.step_key,
        "name": "",
        "type": step.task_type,
        "input": resolved_input,
        "workflow_input": run_service.workflow_input_for_run(run),
        "data_interval": run_service.workflow_input_for_run(run).get("data_interval"),
        "dependency_outputs": dependency_outputs,
        "attempt": step.attempts,
        "retry_limit": step.retry_limit,
        "timeout_seconds": step.timeout_seconds,
        "lease_token": token,
        "lease_expires_at": step.lease_expires_at,
        "deadline_at": step.deadline_at,
        "heartbeat_interval_seconds": heartbeat_interval(),
        "idempotency_key": run_service.idempotency_key_for(run.id, step.step_key, step.attempts),
        "priority": step.priority,
        "queue": step.queue_name,
        "redacted_keys": redacted_keys,
    }


# --------------------------------------------------------------------------- #
# Lease verification
# --------------------------------------------------------------------------- #
def verify_lease(db: Session, task_id: str, *, worker_id: str, lease_token: str) -> tuple[StepRun, WorkflowRun]:
    step = db.get(StepRun, task_id)
    if step is None:
        raise NotFound("This task is no longer available", code="task_not_found")
    if step.worker_id != worker_id or step.status != "running":
        raise Conflict("This task is not currently assigned to you", code="task_not_assigned")
    import hmac

    if not hmac.compare_digest(step.lease_token or "", lease_token):
        raise Forbidden("The lease token for this task is not valid", code="invalid_lease_token")
    now = datetime.now(timezone.utc)
    lease_expires_at = ensure_utc(step.lease_expires_at)
    deadline_at = ensure_utc(step.deadline_at)
    if lease_expires_at is not None and lease_expires_at <= now:
        raise Conflict("This task's lease has expired and the work has been reassigned", code="lease_expired")
    if deadline_at is not None and deadline_at <= now:
        raise Conflict("This task exceeded its deadline and has been reclaimed", code="deadline_exceeded")
    run = db.get(WorkflowRun, step.run_id)
    if run is None:
        raise NotFound("The run for this task no longer exists", code="run_not_found")
    return step, run


def heartbeat(db: Session, *, step: StepRun, run: WorkflowRun, worker: Worker, active_tasks: int = 1) -> dict[str, Any]:
    now = utcnow()
    now_aware = ensure_utc(now)
    deadline_at = ensure_utc(step.deadline_at)
    if run.cancel_requested and not (
        ((step.spec_json or {}).get("failure_handler") or (step.spec_json or {}).get("compensation_only"))
        and run.error
        and run.error.get("code") == "run_timeout"
    ):
        worker.last_seen_at = now
        db.add(WorkerHeartbeat(worker_id=worker.id, active_tasks=active_tasks, recorded_at=now))
        counter("orchestrator_worker_heartbeats_total")
        return {
            "cancel_requested": True,
            "lease_expires_at": step.lease_expires_at,
            "deadline_at": step.deadline_at,
            "seconds_remaining": max(0.0, (deadline_at - now_aware).total_seconds()) if deadline_at else 0.0,
        }
    if deadline_at is None:
        raise Conflict("This task has no active deadline", code="task_not_assigned")

    deadline_naive = deadline_at.replace(tzinfo=None)
    renewed = min(now + timedelta(seconds=settings.lease_seconds), deadline_naive)
    result = cast(
        CursorResult[Any],
        db.execute(
            update(StepRun)
            .where(
                StepRun.id == step.id,
                StepRun.status == "running",
                StepRun.worker_id == worker.id,
                StepRun.lease_token == step.lease_token,
                StepRun.lease_expires_at > now_aware,
                StepRun.deadline_at > now_aware,
            )
            .values(lease_expires_at=renewed, updated_at=now)
            .execution_options(synchronize_session=False)
        )
    )
    try:
        lease_renewed = result.rowcount == 1
    finally:
        result.close()

    if not lease_renewed:
        raise Conflict("This task lease is no longer owned by this worker", code="lease_lost")

    step.lease_expires_at = renewed
    step.updated_at = now
    worker.last_seen_at = now
    db.add(WorkerHeartbeat(worker_id=worker.id, active_tasks=active_tasks, recorded_at=now))
    counter("orchestrator_worker_heartbeats_total")
    db.flush()
    return {
        "cancel_requested": False,
        "lease_expires_at": renewed,
        "deadline_at": step.deadline_at,
        "seconds_remaining": max(0.0, (deadline_at - now_aware).total_seconds()) if deadline_at else 0.0,
    }


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
def complete_task(
    db: Session,
    *,
    step: StepRun,
    run: WorkflowRun,
    worker: Worker,
    output: Any,
    logs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from app.services.artifact_service import store_if_large
    from app.services.workflow_service import redact_secret_values

    safe_output = redact_secret_values(db, run.workflow_id, output)
    stored_output = store_if_large(db, run, step, safe_output)
    check_task_output(stored_output)
    now = utcnow()
    # Redact credential-shaped keys before the output is stored or returned.
    step.output_data = redact(stored_output)
    step.logs = _merge_logs(step.logs, redact_secret_values(db, run.workflow_id, logs or []))
    handler_after_timeout = bool(
        ((step.spec_json or {}).get("failure_handler") or (step.spec_json or {}).get("compensation_only"))
        and run.error
        and run.error.get("code") == "run_timeout"
    )
    step.status = "cancelled" if run.cancel_requested and not handler_after_timeout else "succeeded"
    step.error = None
    step.finished_at = now
    _clear_lease(step, now)
    _refresh_concurrency_gates_for_step(db, step, run)
    _close_attempt(db, step, step.status, output=stored_output)
    if step.status == "succeeded":
        run_service.store_step_cache_result(db, run, step)
    worker.last_seen_at = now
    duration = run_service.duration_seconds(step.started_at, now)
    event_service.emit(
        db, f"step.{step.status}", run_id=run.id, step=step,
        level="warning" if step.status == "cancelled" else "info",
        message="Task completed" if step.status == "succeeded" else "Task completed after cancellation was requested",
        payload={"attempt": step.attempts, "duration_seconds": duration, "worker_id": worker.id},
    )
    counter("orchestrator_steps_settled_total", {"status": step.status})
    if duration is not None:
        from app.core.metrics import observe

        observe("orchestrator_step_duration_seconds", duration, {"task_type": step.task_type or "unknown"})
    run_service.settle_run(db, run)
    db.flush()
    return {"task_id": step.id, "status": step.status, "run_status": run.status, "attempt": step.attempts, "retry_in_seconds": None}


def fail_task(
    db: Session,
    *,
    step: StepRun,
    run: WorkflowRun,
    worker: Worker,
    error: dict[str, Any],
    retryable: bool | None = True,
    logs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from app.services.workflow_service import redact_secret_values

    now = utcnow()
    step.logs = _merge_logs(step.logs, redact_secret_values(db, run.workflow_id, logs or []))
    if retryable is None:
        retryable = classify_error(error)
    classified_error = dict(error)
    classified_error["retryable"] = bool(retryable)
    step.error = redact_secret_values(db, run.workflow_id, classified_error)
    step.finished_at = now
    cancelled = run.cancel_requested and not (
        ((step.spec_json or {}).get("failure_handler") or (step.spec_json or {}).get("compensation_only"))
        and run.error
        and run.error.get("code") == "run_timeout"
    )
    attempts_left = step.attempts <= step.retry_limit
    should_retry = retryable and attempts_left and not cancelled
    if cancelled:
        step.status = "cancelled"
        delay: float | None = None
    elif should_retry:
        step.status = "retrying"
        delay = run_service.retry_delay_seconds(step)
        step.available_at = now + timedelta(seconds=delay)
    else:
        step.status = "failed"
        delay = None
    step.finished_at = now
    _clear_lease(step, now)
    _refresh_concurrency_gates_for_step(db, step, run)
    _close_attempt(db, step, step.status, error=step.error)
    worker.last_seen_at = now
    duration = run_service.duration_seconds(step.started_at, now)
    event_service.emit(
        db, f"step.{step.status}", run_id=run.id, step=step,
        level="error" if step.status == "failed" else "warning",
        message={
            "failed": "Task failed permanently",
            "retrying": f"Task failed; retrying in {delay:.0f}s" if delay is not None else "Task failed; retrying",
            "cancelled": "Task reported a failure after cancellation was requested",
        }[step.status],
        payload={
            "attempt": step.attempts,
            "retry_limit": step.retry_limit,
            "attempts_remaining": max(0, step.retry_limit - step.attempts + 1),
            "error": step.error,
            "duration_seconds": duration,
            "worker_id": worker.id,
        },
    )
    if step.status == "retrying":
        counter("orchestrator_step_retries_total", {"task_type": step.task_type})
        event_service.emit(
            db, EventType.STEP_RETRY_SCHEDULED, run_id=run.id, step=step,
            message=f"Attempt {step.attempts + 1} scheduled",
            payload={"retry_in_seconds": delay, "next_attempt": step.attempts + 1},
        )
    elif step.status == "failed":
        counter("orchestrator_steps_settled_total", {"status": "failed"})
    run_service.settle_run(db, run)
    db.flush()
    return {"task_id": step.id, "status": step.status, "run_status": run.status, "attempt": step.attempts, "retry_in_seconds": delay}


def classify_error(error: dict[str, Any]) -> bool:
    """Server-side fallback for workers that do not classify exceptions."""
    explicit = error.get("retryable")
    if isinstance(explicit, bool):
        return explicit
    status = error.get("status_code", error.get("http_status", error.get("status")))
    if isinstance(status, int) and 400 <= status < 600:
        return status in {408, 409, 425, 429} or status >= 500
    error_type = str(error.get("type", "")).rsplit(".", 1)[-1]
    if error_type in {"ValueError", "TypeError", "KeyError", "UnsupportedTaskType"}:
        return False
    return True


def _clear_lease(step: StepRun, now: datetime) -> None:
    step.lease_token = None
    step.lease_expires_at = None
    step.deadline_at = None
    step.worker_id = None
    step.updated_at = now


def _merge_logs(existing: list[dict[str, Any]] | None, incoming: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    from app.core.security import redact

    merged = list(existing or [])
    for line in incoming or []:
        entry = redact(line if isinstance(line, dict) else {"message": str(line)})
        entry.setdefault("at", utcnow().isoformat() + "Z")
        merged.append(entry)
    return merged[-200:]


def _close_attempt(db: Session, step: StepRun, status: str, *, error: dict[str, Any] | None = None, output: Any = None) -> None:
    attempt = db.scalar(
        select(StepAttempt).where(StepAttempt.step_run_id == step.id, StepAttempt.attempt_no == step.attempts)
    )
    if attempt is None or attempt.status != "running":
        return
    attempt.status = status
    attempt.finished_at = utcnow()
    attempt.error = error
    attempt.output_data = output
    attempt.logs = list(step.logs or [])


# --------------------------------------------------------------------------- #
# Lease expiry recovery
# --------------------------------------------------------------------------- #
def recover_expired_leases(db: Session, *, limit: int = 200) -> int:
    """Reclaim steps whose lease or deadline passed without a result.

    The reclaim UPDATE is conditional on the step still being ``running`` and
    still expired, so it can never clobber a worker that just reported success.
    """
    now = utcnow()
    now_aware = ensure_utc(now)
    expired = list(
        db.scalars(
            select(StepRun)
            .where(
                StepRun.status == "running",
                or_(StepRun.lease_expires_at <= now_aware, StepRun.deadline_at <= now_aware),
            )
            .order_by(StepRun.lease_expires_at)
            .limit(limit)
        ).all()
    )
    recovered = 0
    for step in expired:
        deadline_at = ensure_utc(step.deadline_at)
        timed_out = deadline_at is not None and deadline_at <= now_aware
        run = db.get(WorkflowRun, step.run_id)
        cancelled = bool(
            run
            and run.cancel_requested
            and not (
                ((step.spec_json or {}).get("failure_handler") or (step.spec_json or {}).get("compensation_only"))
                and run.error
                and run.error.get("code") == "run_timeout"
            )
        )
        step.lease_expirations += 1
        from app.core.metrics import counter as _counter

        _counter("orchestrator_lease_expirations_total")
        poisoned = step.lease_expirations >= settings.poison_task_expiry_limit
        if cancelled:
            next_status = "cancelled"
        else:
            next_status = "failed" if poisoned else ("retrying" if step.attempts <= step.retry_limit else "failed")
        error = {
            "code": "poison_task" if poisoned else ("task_timeout" if timed_out else "lease_expired"),
            "message": "Task repeatedly lost its worker and was moved to the dead-letter queue" if poisoned else ("Task exceeded its timeout" if timed_out else "The worker holding this task stopped responding"),
            "attempt": step.attempts,
            "timeout_seconds": step.timeout_seconds if timed_out else None,
        }
        delay = run_service.retry_delay_seconds(step) if next_status == "retrying" else 0.0
        condition = or_(StepRun.lease_expires_at <= now_aware, StepRun.deadline_at <= now_aware)
        result = db.execute(
            update(StepRun)
            .where(StepRun.id == step.id, StepRun.status == "running", condition)
            .execution_options(synchronize_session=False)
            .values(
                status=next_status,
                lease_token=None,
                lease_expires_at=None,
                deadline_at=None,
                worker_id=None,
                lease_expirations=StepRun.lease_expirations + 1,
                available_at=now + timedelta(seconds=delay),
                error=error,
                finished_at=now,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            db.expire(step)
            continue
        step.status = next_status
        step.lease_token = None
        step.lease_expires_at = None
        step.deadline_at = None
        step.worker_id = None
        step.error = error
        _refresh_concurrency_gates_for_step(db, step, run) if run else None
        step.available_at = now + timedelta(seconds=delay)
        step.finished_at = now
        step.updated_at = now
        _close_attempt(db, step, "timed_out" if timed_out else "lease_expired", error=error)
        event_service.emit(
            db,
            EventType.STEP_TIMEOUT if timed_out else EventType.STEP_LEASE_EXPIRED,
            run_id=step.run_id,
            step=step,
            level="error" if next_status == "failed" else "warning",
            message="Task timed out" if timed_out else "Task lease expired",
            payload={"attempt": step.attempts, "next_status": next_status, "retry_in_seconds": delay},
        )
        if timed_out:
            counter("orchestrator_step_timeouts_total", {"task_type": step.task_type})
        if next_status == "retrying":
            counter("orchestrator_step_retries_total", {"task_type": step.task_type})
        if next_status == "retrying":
            enqueue(
                db,
                TOPIC_TASK_READY,
                {"run_id": step.run_id, "step_run_id": step.id, "step_key": step.step_key, "task_type": step.task_type, "reason": "lease_recovery"},
            )
        if run:
            run_service.settle_run(db, run)
        recovered += 1
    return recovered


def worker_running_count(db: Session, worker_id: str) -> int:
    return int(db.scalar(select(func.count()).select_from(StepRun).where(StepRun.worker_id == worker_id, StepRun.status == "running")) or 0)


__all__ = [
    "claim_task",
    "complete_task",
    "fail_task",
    "heartbeat",
    "heartbeat_interval",
    "list_workers",
    "recover_expired_leases",
    "register_worker",
    "release_worker_tasks",
    "set_worker_active",
    "verify_lease",
    "worker_running_count",
    "worker_view",
]
