#!/usr/bin/env python3
"""Migrate SQLite data to Postgres (production hardening, Issue 2).

Reads DATABASE_URL from .env (must be a Postgres URL). Connects to the
SQLite file (source, from SQLITE_SOURCE or ./orchestrator.db) and the
Postgres target. Creates the schema via `alembic upgrade head` against the
target, copies every table in dependency order preserving IDs/timestamps/FKs,
then validates row counts match.

Usage (PowerShell and bash):
    python scripts/migrate_sqlite_to_postgres.py --sqlite ./orchestrator.db

The target DATABASE_URL must be set in .env, e.g.:
    DATABASE_URL=postgresql+psycopg2://user:password@localhost:5432/orchestrator
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


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
    parser = argparse.ArgumentParser(description="Copy SQLite data to Postgres")
    parser.add_argument("--sqlite", default="./orchestrator.db", help="Source SQLite file")
    parser.add_argument("--env", default=".env", help="Env file with target DATABASE_URL")
    args = parser.parse_args()

    root = Path.cwd()
    _load_dotenv(root / args.env)
    target_url = os.environ.get("DATABASE_URL", "")
    if not target_url.startswith("postgresql"):
        print("ERROR: DATABASE_URL in .env must be a postgresql:// URL", file=sys.stderr)
        return 2
    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        print(f"ERROR: SQLite source not found: {sqlite_path}", file=sys.stderr)
        return 2

    print(f"Source: {sqlite_path}")
    print(f"Target: {target_url.split('@')[-1]}")

    # 1. Create schema on Postgres.
    print("\n[1/3] Creating schema via alembic upgrade head...")
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root)
    if result.returncode != 0:
        print("ERROR: alembic upgrade failed", file=sys.stderr)
        return 1

    # 2. Copy tables in dependency order.
    print("\n[2/3] Copying tables...")
    try:
        import sqlalchemy as sa
    except ImportError:
        print("ERROR: sqlalchemy not installed", file=sys.stderr)
        return 2

    src = sa.create_engine(f"sqlite:///{sqlite_path}")
    dst = sa.create_engine(target_url)
    src_meta = sa.MetaData()
    src_meta.reflect(bind=src)

    # Topological order via FK dependencies.
    ordered = list(sa.sql.ddl.sort_tables(src_meta.tables.values()))
    total_rows = 0
    with dst.begin() as conn:
        for table in ordered:
            rows = [dict(r._mapping) for r in src.connect().execute(table.select()).all()]
            if rows:
                conn.execute(table.insert(), rows)
                total_rows += len(rows)
            print(f"  {table.name}: {len(rows)} rows")
    print(f"\nCopied {total_rows} rows across {len(ordered)} tables.")

    # 3. Validate row counts.
    print("\n[3/3] Validating row counts...")
    mismatches = []
    for table in ordered:
        src_count = src.connect().execute(sa.select(sa.func.count()).select_from(table)).scalar()
        with dst.connect() as conn:
            dst_count = conn.execute(sa.select(sa.func.count()).select_from(table)).scalar()
        if src_count != dst_count:
            mismatches.append(f"{table.name}: sqlite={src_count} postgres={dst_count}")
    if mismatches:
        print("MISMATCHES:", file=sys.stderr)
        for m in mismatches:
            print(f"  {m}", file=sys.stderr)
        return 1
    print("All row counts match. Migration complete.")
    print("\nNext: update .env DATABASE_URL to the Postgres URL and restart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
