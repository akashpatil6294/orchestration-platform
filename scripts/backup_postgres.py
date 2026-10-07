#!/usr/bin/env python3
"""Postgres backup via pg_dump (production: backups).

Creates a timestamped compressed dump of the Postgres database configured
by DATABASE_URL. Keeps the newest N backups (default 7), deletes older ones.

Usage (PowerShell and bash):
    python scripts/backup_postgres.py
    python scripts/backup_postgres.py --keep 14 --out D:\\backups

Schedule it:
    # Linux cron (daily 02:00)
    0 2 * * * cd /srv/orchestrator && python scripts/backup_postgres.py >> /var/log/orchestrator-backup.log 2>&1
    # Windows Task Scheduler: run `python scripts/backup_postgres.py` daily,
    # working directory = project root.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up Postgres to a timestamped dump")
    parser.add_argument("--out", default="./backups", help="Backup directory")
    parser.add_argument("--keep", type=int, default=7, help="How many newest backups to keep")
    parser.add_argument("--env", default=".env", help="Env file with DATABASE_URL")
    args = parser.parse_args()

    _load_dotenv(Path.cwd() / args.env)
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        print("ERROR: DATABASE_URL must be a postgresql:// URL (backups are Postgres-only)", file=sys.stderr)
        return 2

    parsed = urlparse(url)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dump_path = out_dir / f"orchestrator-{stamp}.dump"

    env = dict(os.environ)
    if parsed.username:
        env["PGUSER"] = parsed.username
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    if parsed.hostname:
        env["PGHOST"] = parsed.hostname
    if parsed.port:
        env["PGPORT"] = str(parsed.port)

    print(f"Backing up {parsed.hostname}{parsed.path} -> {dump_path}")
    result = subprocess.run(
        ["pg_dump", "-Fc", "-f", str(dump_path), parsed.path.lstrip("/") or "postgres"],
        env=env,
    )
    if result.returncode != 0:
        print("ERROR: pg_dump failed", file=sys.stderr)
        return 1

    size_mb = dump_path.stat().st_size / 1_048_576
    print(f"OK: {dump_path} ({size_mb:.1f} MB)")

    # Retention: keep newest N.
    dumps = sorted(out_dir.glob("orchestrator-*.dump"))
    for old in dumps[:-args.keep] if len(dumps) > args.keep else []:
        old.unlink()
        print(f"Pruned {old.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
