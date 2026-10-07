#!/usr/bin/env python3
"""Chaos experiments for reliability testing (Stage H, H5).

Runs a worker with chaos injection enabled, or drives specific failure
scenarios against a running platform.

Usage:
    # Terminal 1: API server
    uvicorn app.main:app --port 8001

    # Terminal 2: chaos worker (30% random failures, 2s slowdown)
    CHAOS_FAILURE_RATE=0.3 CHAOS_SLOW_SECONDS=2 python scripts/chaos.py worker

    # Terminal 3: run the reliability demo
    python scripts/chaos.py demo --api http://127.0.0.1:8001 --token $WORKER_TOKEN

Scenarios:
    worker   - start a worker with chaos env vars (passes through to sample_worker)
    demo     - create the 3-step reliability demo workflow, publish, and run it
    kill     - simulate worker death mid-run (stops lease heartbeats; the
               platform reclaims the lease and another worker picks it up)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

DEMO_WORKFLOW = {
    "name": "Reliability demo (chaos)",
    "description": "3-step pipeline for chaos testing: fetch -> transform -> store",
    "steps": [
        {"id": "fetch", "type": "demo.echo", "input": {"value": "raw data"}},
        {"id": "transform", "type": "demo.echo", "input": {"value": "{{fetch.output}} processed"}, "depends_on": ["fetch"],
         "policy": {"max_retries": 3, "backoff": "exponential"}},
        {"id": "store", "type": "demo.echo", "input": {"value": "{{transform.output}} stored"}, "depends_on": ["transform"],
         "policy": {"max_retries": 3}},
    ],
}


def api_request(base: str, token: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def cmd_demo(args: argparse.Namespace) -> int:
    base = args.api.rstrip("/")
    token = args.token or os.environ.get("WORKER_TOKEN")
    if not token:
        print("Set --token or WORKER_TOKEN", file=sys.stderr)
        return 2
    # The demo uses the API token for workflow management; workers use WORKER_TOKEN.
    # For simplicity we reuse the same token if it's a user token.
    print("Creating reliability demo workflow...")
    wf = api_request(base, token, "POST", "/api/v1/workflows", DEMO_WORKFLOW)
    workflow_id = wf["id"]
    print(f"  workflow {workflow_id}")
    api_request(base, token, "POST", f"/api/v1/workflows/{workflow_id}/publish", {"note": "chaos demo"})
    print("  published v1")
    run = api_request(base, token, "POST", f"/api/v1/workflows/{workflow_id}/runs", {"input": {}})
    run_id = run["id"]
    print(f"  run {run_id} started — watch it at /runs/{run_id}")
    print()
    print("With chaos enabled on the worker, expect:")
    print("  - random ChaosError failures (retryable) -> automatic retries with backoff")
    print("  - slow steps -> visible in durations")
    print("  - duplicate completions -> rejected idempotently by the server")
    print("  - final status: succeeded (chaos is retryable by design)")
    # Poll until terminal.
    for _ in range(120):
        detail = api_request(base, token, "GET", f"/api/v1/runs/{run_id}")
        status = detail["status"]
        steps = {s["key"]: s["status"] for s in detail.get("steps", [])}
        print(f"  [{status}] {steps}", flush=True)
        if status in ("succeeded", "failed", "cancelled"):
            print(f"Final: {status}")
            return 0 if status == "succeeded" else 1
        time.sleep(5)
    print("Timed out waiting for run completion", file=sys.stderr)
    return 1


def cmd_worker(_args: argparse.Namespace) -> int:
    # Re-exec the sample worker with chaos env vars already set by the caller.
    from app.sample_worker import main

    return main()


def main() -> int:
    parser = argparse.ArgumentParser(description="Chaos experiments for the orchestration platform")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("worker", help="Start a worker with chaos injection (configure via CHAOS_* env vars)")

    demo = sub.add_parser("demo", help="Create and run the 3-step reliability demo")
    demo.add_argument("--api", default="http://127.0.0.1:8001", help="API base URL")
    demo.add_argument("--token", default=None, help="API token (or set WORKER_TOKEN)")

    args = parser.parse_args()
    if args.command == "worker":
        return cmd_worker(args)
    if args.command == "demo":
        return cmd_demo(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
