"""FastAPI application entry point.

    uvicorn app.main:app --reload

Responsibilities kept here: middleware, router registration, lifespan (start and
stop the scheduler and outbox relay), and the startup bootstrap of the first
administrator. Business rules live in ``app.services``.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select

from app.api import alerts, audit, auth, recovery, connections, demo, dlq, documents, health, notifications, ops, runs, saved_filters, schedules, tasks, teams, templates, triggers, workers, workflows
from app.config import settings
from app.core.errors import register_exception_handlers
from app.core.logging import bind_request, configure_logging, get_logger, new_request_id
from app.core.metrics import counter, observe
from app.core.security import hash_password
from app.database import SessionLocal, check_database
from app.models.user import User
from app.services import outbox_service, scheduler_service

logger = get_logger("app.main")


def _init_error_tracking(settings) -> None:
    """Initialize Sentry if SENTRY_DSN is set (production: error tracking).

    The sentry-sdk package is optional; the app boots fine without it.
    """
    if not settings.sentry_dsn.strip():
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; error tracking disabled")
        return
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment or settings.environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        integrations=[FastApiIntegration(), SqlalchemyIntegration()],
    )
    logger.info("Sentry error tracking enabled")


def _log_startup_config(settings) -> None:
    """Log environment posture at startup (production hardening, Issues 1+7).

    Prints environment, whether production-grade secrets are present, the
    database backend, CORS origins, and the embedded worker state — never
    the secret values themselves.
    """
    from app.config import DEV_PEPPER, DEV_SECRET

    secrets_ok = (
        settings.secret_key != DEV_SECRET
        and len(settings.secret_key) >= 32
        and settings.worker_secret_pepper != DEV_PEPPER
        and len(settings.worker_secret_pepper) >= 16
        and bool(settings.secrets_encryption_key)
    )
    logger.info(
        "Startup configuration",
        extra={
            "environment": settings.environment,
            "production_secrets": "present" if secrets_ok else "DEV DEFAULTS (unsafe for production)",
            "database": settings.database_url.split("://")[0],
            "cors_origins": settings.cors_origin_list,
            "embedded_worker": "on" if settings.embedded_worker_on else "off",
        },
    )
    if settings.is_production:
        if settings.is_sqlite:
            logger.warning("SQLite in production: single-writer database will lock under concurrency; migrate to Postgres")
        for origin in settings.cors_origin_list:
            if origin.startswith("http://"):
                logger.warning("CORS origin uses http:// in production", extra={"origin": origin})
        if settings.embedded_worker_on:
            logger.warning(
                "Embedded worker is running in production. This is not recommended "
                "for durable workloads — run a separate worker process instead. "
                "The service will continue anyway."
            )

DESCRIPTION = """
A self-hosted workflow orchestration platform.

* **Workflows** are versioned DAGs. Drafts are edited freely; publishing creates
  an immutable version, and every run pins the version it executed.
* **Runs** execute step by step across independent workers. Dependencies gate
  readiness, retries use exponential backoff, and every step has a timeout.
* **Workers** claim tasks with a lease, heartbeat while they work, and report
  results. Expired leases are reclaimed automatically.
* **Schedules** fire workflows on a timezone-aware cron cadence with duplicate
  protection.

Task types are dispatched by name to a worker's explicit handler allow-list; the
server never evaluates code from a workflow definition.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    ok, error = check_database()
    if not ok:
        logger.error("Database is not reachable at startup", extra={"error": error})
    else:
        logger.info(
            "Orchestration platform starting",
            extra={"environment": settings.environment, "database": settings.database_url.split("://")[0]},
        )
    _bootstrap_admin()
    if settings.supabase_auth_enabled:
        # Print the derived issuer and JWKS URL so a Supabase project-URL
        # typo is visible in the first seconds of the log instead of
        # surfacing later as an infinite login/logout loop.
        logger.info(
            "Supabase issuer: %s",
            settings.supabase_issuer,
        )
        logger.info(
            "Supabase JWKS URL: %s",
            settings.supabase_jwks_url_resolved,
        )
    else:
        logger.info("Supabase sign-in is not configured (SUPABASE_URL is empty)")
    outbox_service.relay.start()
    scheduler_service.loop.start()
    embedded_worker = None
    try:
        from app.worker import embedded as embedded_module

        embedded_worker = embedded_module.start_embedded_worker(app)
    except Exception as exc:  # noqa: BLE001 - the API must stay up without a worker
        logger.warning("Embedded worker failed to start", extra={"error": str(exc)})
    try:
        yield
    finally:
        if embedded_worker is not None:
            try:
                embedded_worker.stop()
            except Exception as exc:  # noqa: BLE001 - shutdown must not hang
                logger.warning("Embedded worker failed to stop cleanly", extra={"error": str(exc)})
        try:
            from app.worker import embedded as embedded_module

            embedded_module.stop_on_demand_worker()
        except Exception as exc:  # noqa: BLE001 - shutdown must not hang
            logger.warning("On-demand embedded worker failed to stop cleanly", extra={"error": str(exc)})
        scheduler_service.loop.stop()
        outbox_service.relay.stop()
        logger.info("Orchestration platform stopped")


def _bootstrap_admin() -> None:
    """Create the configured bootstrap administrator, if any."""
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        return
    with SessionLocal() as db:
        existing = db.scalar(select(User).where(func.lower(User.email) == settings.bootstrap_admin_email.lower()))
        if existing is not None:
            return
        db.add(
            User(
                email=settings.bootstrap_admin_email.lower(),
                display_name=settings.bootstrap_admin_email.split("@")[0],
                password_hash=hash_password(settings.bootstrap_admin_password),
                is_admin=True,
            )
        )
        db.commit()
        logger.info("Bootstrap administrator created", extra={"email": settings.bootstrap_admin_email})


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="Orchestration Platform API",
        version=health.VERSION,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        openapi_tags=[
            {"name": "auth", "description": "Accounts, sessions, API tokens and worker credentials."},
            {"name": "workflows", "description": "Drafts, DAG validation, immutable publishing and versions."},
            {"name": "runs", "description": "Run history, live detail, events, attempts, cancellation and retry."},
            {"name": "schedules", "description": "Timezone-aware recurring schedules."},
            {"name": "triggers", "description": "Signed workflow webhooks and workflow-success triggers."},
            {"name": "webhooks", "description": "HMAC-signed external event ingestion."},
            {"name": "workers", "description": "Worker registration and fleet management."},
            {"name": "tasks", "description": "Lease-bound task heartbeat, completion and failure reporting."},
            {"name": "operations", "description": "Health, readiness, metrics and operator tooling."},
        ],
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-Id", "X-Orchestrator-Timestamp", "X-Orchestrator-Signature"],
        expose_headers=["X-Request-Id", "X-Process-Time"],
    )

    # Production hardening: startup visibility (Issues 1, 7). Log environment,
    # secret posture, and CORS origins — never the values themselves.
    _init_error_tracking(settings)
    _log_startup_config(settings)

    @app.middleware("http")
    async def _limit_body(request: Request, call_next) -> Response:
        # The document upload endpoint streams to disk and enforces its own
        # (higher) size limit while reading, so it is exempt from the generic
        # body cap — otherwise a legitimate 5 MB upload would be rejected
        # before the endpoint ever sees it.
        if request.url.path == "/api/v1/documents" and request.method == "POST":
            return await call_next(request)
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > settings.max_request_bytes:
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "payload_too_large",
                        "message": f"Request body exceeds the {settings.max_request_bytes} byte limit",
                    }
                },
            )
        return await call_next(request)

    @app.middleware("http")
    async def _limit_json_depth(request: Request, call_next) -> Response:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            body = await request.body()
            if body:
                try:
                    import json as _json

                    payload = _json.loads(body)

                    def _depth(value: object, current: int) -> int:
                        if current > settings.max_json_depth:
                            return current
                        if isinstance(value, dict):
                            return max((_depth(v, current + 1) for v in value.values()), default=current)
                        if isinstance(value, list):
                            return max((_depth(v, current + 1) for v in value), default=current)
                        return current

                    if _depth(payload, 1) > settings.max_json_depth:
                        return JSONResponse(
                            status_code=413,
                            content={
                                "error": {
                                    "code": "payload_too_deep",
                                    "message": f"JSON nesting exceeds the {settings.max_json_depth} level limit",
                                }
                            },
                        )
                except ValueError:
                    pass  # Invalid JSON is handled by FastAPI's validation.
        return await call_next(request)

    @app.middleware("http")
    async def _rate_limit_api(request: Request, call_next) -> Response:
        # Production: rate limiting. Off by default (0); set
        # API_RATE_LIMIT_PER_MINUTE in production. Health probes are exempt.
        limit = settings.api_rate_limit_per_minute
        if limit > 0 and not request.url.path.startswith(("/health", "/ready")):
            from app.core.rate_limit import api_limiter

            limiter = api_limiter()
            limiter.max_hits = limit
            client_ip = request.client.host if request.client else "unknown"
            allowed, retry_after = limiter.allow(f"api:{client_ip}")
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                    content={"error": {"code": "rate_limited", "message": "Too many requests"}},
                )
        return await call_next(request)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    @app.middleware("http")
    async def _observability(request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-Id") or new_request_id()
        bind_request(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = time.perf_counter() - started
            counter("orchestrator_http_requests_total", {"method": request.method, "route": request.url.path, "status": "500"})
            observe("orchestrator_http_request_seconds", duration, {"method": request.method, "route": request.url.path})
            raise
        duration = time.perf_counter() - started
        route = _route_template(request)
        counter("orchestrator_http_requests_total", {"method": request.method, "route": route, "status": str(response.status_code)})
        observe("orchestrator_http_request_seconds", duration, {"method": request.method, "route": route})
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Process-Time"] = f"{duration:.4f}"
        if duration * 1000 > settings.slow_query_ms:
            logger.warning("Slow request", extra={"path": request.url.path, "duration_ms": round(duration * 1000, 1)})
        return response

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(documents.router)
    app.include_router(templates.router)
    app.include_router(connections.router)
    app.include_router(saved_filters.router)
    app.include_router(auth.router)
    app.include_router(workflows.router)
    app.include_router(schedules.workflow_router)
    app.include_router(triggers.workflow_router)
    app.include_router(runs.router)
    app.include_router(schedules.router)
    app.include_router(triggers.router)
    app.include_router(triggers.hook_router)
    app.include_router(workers.router)
    app.include_router(tasks.router)
    app.include_router(tasks.task_types_router)
    app.include_router(dlq.router)
    app.include_router(demo.router)
    app.include_router(teams.router)
    app.include_router(audit.router)
    app.include_router(alerts.router)
    app.include_router(recovery.router)
    app.include_router(notifications.router)
    app.include_router(ops.router)

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/docs")

    return app


def _route_template(request: Request) -> str:
    """Collapse path parameters so metric labels stay bounded."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or "unmatched"


app = create_app()

__all__ = ["app", "create_app"]
