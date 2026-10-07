"""Measure worker claim throughput against an existing ready-task backlog.

Create a published workflow with many independent steps first, register the
worker, and set WORKER_ID / WORKER_TOKEN. Claimed tasks are completed so the
backlog keeps draining and the measured rate reflects real dispatch plus
result reporting; pass --no-complete to hold leases instead. Whatever is still
held when the measurement ends is released with the worker shutdown endpoint.

Reported numbers
----------------
* claim requests/sec and claims/sec
* client-observed claim latency p50/p95 (milliseconds)
* duplicate claims (a task handed out for a set the worker said it already
  holds, or twice in one response) -- must stay 0; re-sent live assignments
  caused by concurrent snapshots are reported separately
* dispatch backend (sqlite/postgresql) and the number of concurrent claimers

Exit status is non-zero when any request errored or a duplicate claim occurred.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def _percentile(samples: list[float], fraction: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=os.getenv("ORCHESTRATOR_API", "http://127.0.0.1:8000"))
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=8, help="Concurrent claim requests from this registered worker")
    parser.add_argument("--wait-seconds", type=int, default=1, choices=range(0, 26))
    parser.add_argument(
        "--no-complete",
        action="store_true",
        help="Leave claimed tasks running instead of reporting success (measures pure claim dispatch)",
    )
    args = parser.parse_args()
    worker_id = os.getenv("WORKER_ID", "")
    token = os.getenv("WORKER_TOKEN", "")
    if not worker_id or not token:
        parser.error("Set WORKER_ID and WORKER_TOKEN in the environment")
    if args.duration <= 0 or args.workers < 1 or args.workers > 64:
        parser.error("duration must be positive and workers must be between 1 and 64")

    base = args.api.rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}
    queues = [item.strip() for item in os.getenv("WORKER_QUEUES", "default").split(",") if item.strip()]
    stop = threading.Event()
    lock = threading.Lock()
    known_ids: set[str] = set()
    finished_ids: set[str] = set()
    tasks_claimed = 0
    tasks_finished = 0
    requests_completed = 0
    duplicate_claims = 0
    recovered_unadvertised = 0
    errors: list[str] = []
    latencies: list[float] = []

    def post(path: str, body: dict[str, Any], timeout: float = 35.0) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{base}{path}", data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")

    def get(path: str, timeout: float = 5.0) -> dict[str, Any]:
        request = urllib.request.Request(f"{base}{path}", headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")

    def complete(task: dict[str, Any]) -> None:
        nonlocal tasks_finished
        task_id = str(task["id"])
        with lock:
            if task_id in finished_ids:
                # The platform re-sent a live assignment; a real worker would not
                # execute or report it twice.
                return
            finished_ids.add(task_id)
        try:
            post(
                f"/api/v1/tasks/{task['id']}/complete",
                {"worker_id": worker_id, "lease_token": task["lease_token"], "output": {"benchmark": True}},
                timeout=10.0,
            )
            with lock:
                tasks_finished += 1
                known_ids.discard(task_id)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            with lock:
                finished_ids.discard(task_id)
                errors.append(f"complete:{type(exc).__name__}")

    def claimant() -> None:
        nonlocal tasks_claimed, requests_completed, duplicate_claims, recovered_unadvertised
        while not stop.is_set():
            with lock:
                known = sorted(known_ids)[:64]
            advertised = set(known)
            started = time.perf_counter()
            try:
                response = post(
                    "/api/v1/workers/claim",
                    {
                        "worker_id": worker_id,
                        "available_slots": 1,
                        "queues": queues,
                        "in_flight_task_ids": known,
                        "wait_seconds": args.wait_seconds,
                    },
                    timeout=max(10.0, args.wait_seconds + 10.0),
                )
            except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
                elapsed = time.perf_counter() - started
                with lock:
                    errors.append(type(exc).__name__)
                    latencies.append(elapsed)
                # Keep measuring: a single slow request must not end the run.
                continue
            elapsed = time.perf_counter() - started
            assignments = response.get("tasks") or ([response["task"]] if response.get("task") else [])
            with lock:
                requests_completed += 1
                latencies.append(elapsed)
                seen_in_response: set[str] = set()
                for task in assignments:
                    task_id = str(task["id"])
                    if task_id in advertised or task_id in seen_in_response:
                        # The platform handed out a task this exact request said
                        # the worker already holds: a real duplicate claim.
                        duplicate_claims += 1
                    elif task_id in known_ids:
                        # A live assignment re-sent because a concurrent claim
                        # recorded it after this request's snapshot was taken.
                        recovered_unadvertised += 1
                    seen_in_response.add(task_id)
                    known_ids.add(task_id)
                tasks_claimed += len(assignments)
            if not args.no_complete:
                for task in assignments:
                    complete(task)

    task_types = [item.strip() for item in os.getenv("WORKER_TASK_TYPES", "").split(",") if item.strip()]
    registered = post(
        "/api/v1/workers/register",
        {
            "worker_id": worker_id,
            "name": f"claim-load-{worker_id}",
            "task_types": task_types,
            "queues": queues,
            "max_concurrency": min(64, args.workers),
            "metadata": {"benchmark": "load_test_claims"},
        },
    )
    print(f"Registered worker (active={registered.get('active')}, queues={registered.get('queues')})")

    backend = "unknown"
    try:
        backend = str(get("/ready").get("database") or "unknown")
    except Exception as exc:  # pragma: no cover - depends on the deployment under test
        errors.append(f"backend_probe:{type(exc).__name__}")

    started = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="claim-load") as pool:
            futures = [pool.submit(claimant) for _ in range(args.workers)]
            while time.perf_counter() - started < args.duration and not stop.wait(0.05):
                pass
            elapsed = time.perf_counter() - started
            stop.set()
            for future in futures:
                future.result()
    finally:
        try:
            post(f"/api/v1/workers/{worker_id}/shutdown", {}, timeout=10)
        except Exception:
            pass

    elapsed = max(0.001, elapsed)
    with lock:
        p50 = _percentile(latencies, 0.50) * 1000
        p95 = _percentile(latencies, 0.95) * 1000
        mean = statistics.fmean(latencies) * 1000 if latencies else 0.0
    print("=== claim load test ===")
    print(f"Dispatch backend: {backend}")
    print(f"Claim worker: {worker_id} | queues: {','.join(queues)} | concurrent claimers: {args.workers}")
    print(f"Measurement seconds: {elapsed:.2f}")
    print(f"Claim requests: {requests_completed} ({requests_completed / elapsed:.2f} requests/sec)")
    print(f"Tasks claimed: {tasks_claimed} ({tasks_claimed / elapsed:.2f} claims/sec)")
    print(f"Tasks completed: {tasks_finished}")
    print(f"Claim latency: p50={p50:.2f}ms p95={p95:.2f}ms mean={mean:.2f}ms (n={len(latencies)})")
    print(f"Duplicate claims: {duplicate_claims} (must be 0)")
    print(f"Re-sent live assignments (concurrent snapshot, not a duplicate): {recovered_unadvertised}")
    print(f"Tasks still held at shutdown (released by the endpoint): {len(known_ids)}")
    print(f"Request errors: {len(errors)}" + (f" {sorted(set(errors))}" if errors else ""))
    return 0 if not errors and duplicate_claims == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
