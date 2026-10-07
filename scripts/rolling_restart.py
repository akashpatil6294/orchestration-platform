#!/usr/bin/env python3
"""Rolling restart of the API processes (production: zero-downtime deploys).

Restarts API processes one at a time, waiting for each to become healthy
before moving to the next. Requires the Caddy multi-backend block (see
Caddyfile) so traffic keeps flowing to the survivors.

Usage (PowerShell and bash):
    # With two API processes managed by PID files:
    python scripts/rolling_restart.py --pids /run/orchestrator/api-8001.pid /run/orchestrator/api-8002.pid \
        --start-cmd "python -m uvicorn app.main:app --host 127.0.0.1 --port {port}"

Each PID file must sit next to a port mapping: the script derives the port
from the filename (api-<port>.pid). The start command may use {port}.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen


def _wait_healthy(port: int, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(2)
    return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Rolling restart of API processes")
    parser.add_argument("--pids", nargs="+", required=True, help="PID files (api-<port>.pid)")
    parser.add_argument("--start-cmd", required=True, help="Command to start one API ({port} placeholder)")
    parser.add_argument("--timeout", type=int, default=60, help="Seconds to wait for health per process")
    args = parser.parse_args()

    for pid_file in args.pids:
        path = Path(pid_file)
        try:
            port = int(path.stem.split("-")[-1])
        except ValueError:
            print(f"ERROR: cannot derive port from {pid_file} (expected api-<port>.pid)", file=sys.stderr)
            return 2
        if not path.exists():
            print(f"SKIP: no PID file {pid_file}")
            continue
        pid = int(path.read_text().strip())
        print(f"[{port}] stopping pid {pid}...")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            print(f"[{port}] already stopped")
        for _ in range(30):
            if not _pid_alive(pid):
                break
            time.sleep(1)
        print(f"[{port}] starting: {args.start_cmd.format(port=port)}")
        proc = subprocess.Popen(args.start_cmd.format(port=port), shell=True, cwd=Path.cwd())
        path.write_text(str(proc.pid))
        # The child re-execs via shell; give it a moment then check health.
        time.sleep(3)
        if not _wait_healthy(port, args.timeout):
            print(f"ERROR: [{port}] did not become healthy in {args.timeout}s — aborting", file=sys.stderr)
            return 1
        print(f"[{port}] healthy")
    print("Rolling restart complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
