#!/usr/bin/env python3
"""Uptime probe (production: monitoring/alerting).

Polls /health and /ready, exits 0 when both pass, 1 otherwise. Designed for
cron / Task Scheduler / UptimeRobot-style checks. Pair with an external
alerter (UptimeRobot, Better Stack, PagerDuty) for notifications — this
script only reports status.

Usage (PowerShell and bash):
    python scripts/healthcheck.py
    python scripts/healthcheck.py --base http://127.0.0.1:8001 --timeout 10

Cron (every minute, alert on failure via mail):
    * * * * * cd /srv/orchestrator && python scripts/healthcheck.py || echo "API DOWN" | mail -s "orchestrator down" ops@example.com
"""
from __future__ import annotations

import argparse
import json
import sys
from urllib.request import urlopen


def _check(url: str, timeout: int) -> tuple[bool, str]:
    try:
        with urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return True, "ok (non-JSON)"
            # /ready reports component status; fail if anything is down.
            checks = data.get("checks", {})
            bad = [k for k, v in checks.items() if isinstance(v, dict) and v.get("status") not in (None, "ok", "healthy", "up")]
            if bad:
                return False, f"unhealthy components: {', '.join(bad)}"
            return True, "ok"
    except Exception as exc:  # noqa: BLE001 - probe must report, not crash
        return False, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe /health and /ready")
    parser.add_argument("--base", default="http://127.0.0.1:8001", help="API base URL")
    parser.add_argument("--timeout", type=int, default=10, help="Seconds per probe")
    args = parser.parse_args()

    failed = False
    for endpoint in ("/health", "/ready"):
        ok, detail = _check(args.base.rstrip("/") + endpoint, args.timeout)
        print(f"{endpoint}: {'OK' if ok else 'FAIL'} ({detail})")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
