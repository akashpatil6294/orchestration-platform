"""Operator CLI.

    python -m app.cli migrate                 # apply schema migrations
    python -m app.cli create-user             # create an account
    python -m app.cli create-worker           # mint a worker credential
    python -m app.cli seed-demo               # install samples and a webhook trigger
    python -m app.cli gen-secret              # generate SECRET_KEY material
    python -m app.cli task-types              # list the worker handler allow-list
    python -m app.cli doctor                  # check configuration and connectivity

Everything here is non-interactive when the required flags are supplied, so it
works in scripts and containers.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from typing import Any

from sqlalchemy import func, select

from app.config import settings
from app.core.security import generate_token, hash_password, hash_token, token_prefix
from app.database import SessionLocal, check_database
from app.models.user import User
from app.models.worker import Worker
from app.services import demo_data


def _session():
    return SessionLocal()


def cmd_migrate(args: argparse.Namespace) -> int:
    """Apply Alembic migrations (or create tables when Alembic is unavailable)."""
    from alembic import command
    from alembic.config import Config

    try:
        config = Config("alembic.ini")
        config.set_main_option("script_location", "migrations")
        command.upgrade(config, args.revision)
        print(f"Migrations applied: {args.revision}")

    except Exception as exc:  # pragma: no cover - depends on the environment
        if not args.create_if_missing:
            print(f"Migration failed: {exc}", file=sys.stderr)
            return 1
        from app.models import Base
        from app.database import engine

        print(f"Alembic unavailable ({type(exc).__name__}); creating tables directly")
        Base.metadata.create_all(engine)
        print("Schema created")
    return 0


def cmd_create_user(args: argparse.Namespace) -> int:
    with _session() as db:
        email = args.email.lower()
        existing = db.scalar(select(User).where(func.lower(User.email) == email))
        if existing is not None:
            print(f"An account already exists for {email}", file=sys.stderr)
            return 1
        is_first = int(db.scalar(select(func.count()).select_from(User)) or 0) == 0
        password = args.password or secrets.token_urlsafe(12)
        user = User(
            email=email,
            display_name=args.name or email.split("@")[0],
            password_hash=hash_password(password),
            is_admin=args.admin or is_first,
        )
        db.add(user)
        db.commit()
        print(f"Created user {email} (admin={user.is_admin})")
        if not args.password:
            print(f"Generated password: {password}")
        return 0


def cmd_create_worker(args: argparse.Namespace) -> int:
    with _session() as db:
        token = generate_token("wrk")
        worker = db.get(Worker, args.worker_id)
        if worker is None:
            worker = Worker(id=args.worker_id, name=args.worker_id)
            db.add(worker)
        worker.token_hash = hash_token(token)
        worker.token_prefix = token_prefix(token)
        worker.active = True
        worker.max_concurrency = args.concurrency
        if args.task_types:
            worker.task_types = [item.strip() for item in args.task_types.split(",") if item.strip()]
        if args.email:
            owner = db.scalar(select(User).where(func.lower(User.email) == args.email.lower()))
            if owner is None:
                print(f"No account found for {args.email}", file=sys.stderr)
                return 1
            worker.owner_id = owner.id
        db.commit()
        if args.token_file:
            with open(args.token_file, "w", encoding="utf-8") as handle:
                handle.write(token)
            print(f"Worker id:    {worker.id}")
            print(f"Worker token written to {args.token_file}")
        else:
            print(f"Worker id:    {worker.id}")
            print(f"Worker token: {token}")
            print("Set it as WORKER_TOKEN in the worker environment. It is not stored in plaintext.")
        return 0


def cmd_seed_demo(args: argparse.Namespace) -> int:
    with _session() as db:
        owner = db.scalar(select(User).where(func.lower(User.email) == args.email.lower())) if args.email else db.scalar(select(User).order_by(User.created_at))
        if owner is None:
            print("Create an account first: python -m app.cli create-user --email you@example.com", file=sys.stderr)
            return 1
        workflows = demo_data.install_demo_workflows(db, owner.id)
        trigger = demo_data.install_demo_webhook_trigger(db, owner.id)
        db.commit()
        for workflow in workflows:
            print(f"Installed demo workflow '{workflow.name}' (id={workflow.id}, version={workflow.latest_version})")
        print(f"Seeded webhook endpoint: {settings.public_base_url.rstrip('/')}/api/v1/hooks/{trigger.id}")
        print("Open the Triggers page and rotate its signing secret to reveal a one-time key for local testing.")
        print("Open the UI to explore retries, branching, PDF fan-out, approvals, compensation and webhook triggers.")
        return 0


def cmd_gen_secret(args: argparse.Namespace) -> int:
    print(secrets.token_urlsafe(args.bytes))
    return 0


def cmd_task_types(args: argparse.Namespace) -> int:
    from app.worker import handlers as _handlers  # noqa: F401
    from app.worker.registry import registry

    rows = registry.describe()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{len(rows)} handler(s) registered:\n")
    for row in rows:
        marker = " [side effects]" if row["side_effects"] else ""
        print(f"  {row['task_type']:<22} {row['description']}{marker}")
    print(f"\nWorker allow-list: {','.join(registry.types())}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    if getattr(args, "strict", False):
        return cmd_doctor_strict()
    ok, error = check_database()
    print(f"Database      : {settings.database_url.split('://')[0]} ({'reachable' if ok else 'UNREACHABLE'})")
    if not ok:
        print(f"                {error}")
    print(f"Environment   : {settings.environment}")
    print(f"Dispatch      : {'redis outbox' if settings.redis_url else 'database polling'}")
    print(f"Scheduler     : {'enabled' if settings.scheduler_enabled else 'disabled'} every {settings.scheduler_interval_seconds}s")
    print(f"Lease seconds : {settings.lease_seconds}")
    print(f"Secret key    : {'default (unsafe for production)' if settings.secret_key.startswith('dev-only') else 'customised'}")
    _report_auth_configuration()
    if ok:
        with _session() as db:
            users = int(db.scalar(select(func.count()).select_from(User)) or 0)
            workers = int(db.scalar(select(func.count()).select_from(Worker)) or 0)
        print(f"Accounts      : {users}")
        print(f"Workers       : {workers}")
    return 0 if ok else 1


def cmd_doctor_strict() -> int:
    """Production safety gate (Issue: Prevention 1).

    Fails (exit 1) if ENVIRONMENT=production and any dev default remains.
    Prints a PASS/FAIL table. Safe to run in CI with a fake .env.
    """
    from app.config import DEV_PEPPER, DEV_SECRET

    checks: list[tuple[str, bool, str]] = []
    is_prod = settings.is_production

    # Only enforce production rules when ENVIRONMENT=production.
    if not is_prod:
        print("Environment is not production; --strict passes trivially.")
        print(f"  ENVIRONMENT={settings.environment}")
        return 0

    secrets_ok = (
        settings.secret_key != DEV_SECRET
        and len(settings.secret_key) >= 32
        and settings.worker_secret_pepper != DEV_PEPPER
        and len(settings.worker_secret_pepper) >= 16
        and bool(settings.secrets_encryption_key)
    )
    checks.append(("Production secrets", secrets_ok, "regenerate SECRET_KEY, WORKER_SECRET_PEPPER, SECRETS_ENCRYPTION_KEY"))

    db_ok = not settings.is_sqlite
    checks.append(("Postgres database", db_ok, "set DATABASE_URL to postgresql:// (sqlite locks under concurrency)"))

    cors_ok = all(not o.startswith("http://") for o in settings.cors_origin_list)
    checks.append(("HTTPS CORS origins", cors_ok, "CORS_ORIGINS must use https:// in production"))

    ai_ok = True
    ai_reason = ""
    if settings.ai_provider == "groq" and not settings.groq_api_key.strip():
        ai_ok, ai_reason = False, "GROQ_API_KEY empty with AI_PROVIDER=groq"
    if settings.ai_provider == "gemini" and not settings.gemini_api_key.strip():
        ai_ok, ai_reason = False, "GEMINI_API_KEY empty with AI_PROVIDER=gemini"
    checks.append(("AI provider key", ai_ok, ai_reason or "present"))

    worker_ok = not settings.embedded_worker_on
    checks.append(("Standalone worker", worker_ok, "set EMBEDDED_WORKER_ENABLED=false; run scripts/run_worker.py"))

    failed = False
    print(f"{'CHECK':<22} {'RESULT':<6} DETAIL")
    print("-" * 70)
    for name, ok, detail in checks:
        status = "PASS" if ok else "FAIL"
        print(f"{name:<22} {status:<6} {detail if not ok else ''}")
        if not ok:
            failed = True
    print("-" * 70)
    # Advisory (never fails the gate): cost controls live in the app's
    # workflow budgets + alert rules; cloud spend caps are infra-level.
    print("ADVISORY: configure workflow budgets (/alerts) and cloud spend")
    print("          caps (AWS Budgets / GCP budgets) separately — see SETUP_NOTES.md")
    print("RESULT: " + ("PASS — production-safe" if not failed else "FAIL — fix the items above"))
    return 1 if failed else 0


def _report_auth_configuration() -> None:
    """Report the sign-in configuration. Never prints a secret value."""

    from app.auth.supabase import describe_configuration

    auth = describe_configuration()
    methods = ["email + password"] if auth["password_auth_enabled"] else []
    if auth["enabled"]:
        methods.append(f"Google via Supabase ({auth['verification']})")
    print(f"Sign-in       : {' + '.join(methods) if methods else 'none configured'}")
    if auth["enabled"]:
        print(f"Supabase      : {auth['project_url']}")
        print(f"  issuer      : {auth['issuer']}")
        print(f"  anon key    : {'set' if auth['anon_key_configured'] else 'not set'}")
        print("  shared secret: " + ("set" if auth["verification"] == "shared-secret" else "not set"))
        if auth["privileged_key_present"]:
            print(
                "  WARNING     : SUPABASE_ANON_KEY holds a service-role key. Replace it with the\n"
                "                publishable/anon key; a service-role key bypasses row-level security."
            )


def cmd_rotate_secrets_key(args: argparse.Namespace) -> int:
    """Re-encrypt all stored secrets with a new encryption key.

    Reads the old key from SECRETS_ENCRYPTION_KEY (or --old-key), generates or
    accepts a new key (--new-key), decrypts every stored secret with the old
    key and re-encrypts with the new one. Print the new key so the operator can
    store it in the environment; the old key must be replaced afterwards.
    """
    import secrets as _secrets

    from app.config import settings
    from app.models.connection import Connection
    from app.models.notification import NotificationChannel
    from app.models.workflow import WorkflowSecret, WorkflowTrigger
    from sqlalchemy import select

    old_key = args.old_key or settings.secrets_encryption_key
    if not old_key:
        print("No old key: set SECRETS_ENCRYPTION_KEY or pass --old-key")
        return 1
    new_key = args.new_key or _secrets.token_urlsafe(32)

    db = _session()
    try:
        # Decrypt with old key.
        settings.secrets_encryption_key = old_key
        from app.core import security as _sec

        plaintexts: list[tuple[str, str, str]] = []  # (kind, id, value)
        for row in db.scalars(select(WorkflowSecret)).all():
            plaintexts.append(("workflow_secret", row.id, _sec.decrypt_secret(row.ciphertext)))
        for row in db.scalars(select(WorkflowTrigger).where(WorkflowTrigger.secret_ciphertext.is_not(None))).all():
            plaintexts.append(("trigger_secret", row.id, _sec.decrypt_secret(row.secret_ciphertext)))
        for row in db.scalars(select(NotificationChannel)).all():
            plaintexts.append(("notification_channel", row.id, _sec.decrypt_secret(row.url_ciphertext)))
        for row in db.scalars(select(Connection)).all():
            plaintexts.append(("connection", row.id, _sec.decrypt_secret(row.value_ciphertext)))

        if args.dry_run:
            print(f"Dry run: {len(plaintexts)} secrets would be re-encrypted; nothing was written.")
            return 0

        # Re-encrypt with new key.
        settings.secrets_encryption_key = new_key
        for kind, row_id, value in plaintexts:
            new_ciphertext = _sec.encrypt_secret(value)
            if kind == "workflow_secret":
                db.get(WorkflowSecret, row_id).ciphertext = new_ciphertext
            elif kind == "trigger_secret":
                db.get(WorkflowTrigger, row_id).secret_ciphertext = new_ciphertext
            elif kind == "notification_channel":
                db.get(NotificationChannel, row_id).url_ciphertext = new_ciphertext
            else:
                db.get(Connection, row_id).value_ciphertext = new_ciphertext
        db.commit()
    finally:
        db.close()

    print(f"Rotated {len(plaintexts)} secrets.")
    print("Store this as the new SECRETS_ENCRYPTION_KEY:")
    print(new_key)
    return 0


def cmd_dev(args: argparse.Namespace) -> int:
    """One-command development start: migrate, seed demos if empty, serve."""
    import os

    os.environ.setdefault("ENVIRONMENT", "development")
    print("Applying migrations...")
    rc = cmd_migrate(argparse.Namespace(revision="head", create_if_missing=True))
    if rc != 0:
        return rc
    if not args.no_seed:
        with _session() as db:
            from app.models.workflow import Workflow as WorkflowModel

            has_workflows = db.scalar(select(func.count()).select_from(WorkflowModel)) or 0
            owner = db.scalar(select(User).order_by(User.created_at))
        if has_workflows:
            print("Workflows already exist; skipping demo seed.")
        elif owner is None:
            print("No account yet; skipping demo seed (create one: python -m app.cli create-user --email you@example.com).")
        else:
            print("Seeding demo workflows...")
            rc = cmd_seed_demo(argparse.Namespace(email=owner.email))
            if rc != 0:
                return rc
    print(f"Starting API with embedded worker at http://{args.host}:{args.port} (docs: /docs)")
    print("Frontend dev server (separate terminal): cd frontend && npm run dev")
    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Orchestration platform operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    migrate = sub.add_parser("migrate", help="Apply database migrations")
    migrate.add_argument("--revision", default="head")
    migrate.add_argument("--create-if-missing", action="store_true", default=True)
    migrate.set_defaults(func=cmd_migrate)

    user = sub.add_parser("create-user", help="Create an account")
    user.add_argument("--email", required=True)
    user.add_argument("--password", default="")
    user.add_argument("--name", default="")
    user.add_argument("--admin", action="store_true")
    user.set_defaults(func=cmd_create_user)

    worker = sub.add_parser("create-worker", help="Mint a worker credential")
    worker.add_argument("--worker-id", required=True)
    worker.add_argument("--task-types", default="")
    worker.add_argument("--concurrency", type=int, default=4)
    worker.add_argument("--email", default="", help="Account that owns the worker")
    worker.add_argument("--token-file", default="", help="Write only the raw token to this file (for container bootstrap)")
    worker.set_defaults(func=cmd_create_worker)

    seed = sub.add_parser("seed-demo", help="Install sample workflows and a webhook trigger")
    seed.add_argument("--email", default="")
    seed.set_defaults(func=cmd_seed_demo)

    secret = sub.add_parser("gen-secret", help="Generate random secret material")
    secret.add_argument("--bytes", type=int, default=32)
    secret.set_defaults(func=cmd_gen_secret)

    types = sub.add_parser("task-types", help="List the worker handler allow-list")
    types.add_argument("--json", action="store_true")
    types.set_defaults(func=cmd_task_types)

    doctor = sub.add_parser("doctor", help="Check configuration and connectivity")
    doctor.add_argument("--strict", action="store_true", help="Fail if not production-safe (for CI)")
    doctor.set_defaults(func=cmd_doctor)

    dev = sub.add_parser("dev", help="Migrate, seed demos if empty, and start the API with the embedded worker")
    dev.add_argument("--host", default="127.0.0.1")
    dev.add_argument("--port", type=int, default=8000)
    dev.add_argument("--no-seed", action="store_true", help="Skip demo seeding")
    dev.set_defaults(func=cmd_dev)

    rotate = sub.add_parser("rotate-secrets-key", help="Re-encrypt stored secrets with a new key")
    rotate.add_argument("--old-key", default="")
    rotate.add_argument("--new-key", default="")
    rotate.add_argument("--dry-run", action="store_true", help="Decrypt with the old key and report counts without writing")
    rotate.set_defaults(func=cmd_rotate_secrets_key)

    return parser



def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result: Any = args.func(args)
    return int(result or 0)


if __name__ == "__main__":
    sys.exit(main())
