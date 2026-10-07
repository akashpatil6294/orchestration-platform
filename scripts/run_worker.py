#!/usr/bin/env python3
"""Standalone worker process (production hardening, Issue 5).

Starts a worker using the existing worker code path, without the embedded
flag. Run one or more of these alongside the API in production:

    python scripts/run_worker.py

Environment (see .env.example):
    ORCHESTRATOR_API   base URL of the API (default http://127.0.0.1:8001)
    WORKER_TOKEN       credential from `python -m app.cli create-worker`
    WORKER_ID          worker identity (default standalone-worker-1)
"""
from __future__ import annotations

import os
import sys

# Force a distinct default worker id so it never collides with the sample worker.
os.environ.setdefault("WORKER_ID", "standalone-worker-1")

from app.sample_worker import main

if __name__ == "__main__":
    sys.exit(main())
