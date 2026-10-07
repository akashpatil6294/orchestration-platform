#!/usr/bin/env python3
"""Restore a Postgres backup (production: backups).

Restores a dump created by scripts/backup_postgres.py into the database
configured by DATABASE_URL. Refuses to run against SQLite.

Usage (PowerShell and bash):
    python scripts/restore_postgres.py ./backups/orchestrator-20261007T020000Z.dump

WARNING: this overwrites the target database. The script asks for
confirmation unless --yes is passed.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
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
    parser = argparse.ArgumentParser(description="Restore a Postgres dump")
    parser.add_argument("dump", help="Path to the .dump file")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation")
    parser.add_argument("--env", default=".env", help="Env file with DATABASE_URL")
    args = parser.parse_args()

    dump = Path(args.dump)
    if not dump.exists():
        print(f"ERROR: dump not found: {dump}", file=sys.stderr)
        return 2

    _load_dotenv(Path.cwd() / args.env)
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        print("ERROR: DATABASE_URL must be a postgresql:// URL", file=sys.stderr)
        return 2

    parsed = urlparse(url)
    target = f"{parsed.hostname}{parsed.path}"
    if not args.yes:
        answer = input(f"Overwrite Postgres database {target}? Type YES to confirm: ")
        if answer.strip() != "YES":
            print("Aborted.")
            return 0

    env = dict(os.environ)
    if parsed.username:
        env["PGUSER"] = parsed.username
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    if parsed.hostname:
        env["PGHOST"] = parsed.hostname
    if parsed.port:
        env["PGPORT"] = str(parsed.port)

    print(f"Restoring {dump} -> {target}")
    result = subprocess.run(
        ["pg_restore", "--clean", "--if-exists", "-d", parsed.path.lstrip("/") or "postgres", str(dump)],
        env=env,
    )
    if result.returncode != 0:
        print("ERROR: pg_restore failed", file=sys.stderr)
        return 1
    print("OK: restore complete. Run `alembic upgrade head` to ensure schema is current.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
