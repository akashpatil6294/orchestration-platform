"""Application configuration.

All settings are environment-driven (see ``.env.example``). Values are read once
at import time into a cached :class:`Settings` instance so that runtime code can
depend on ``get_settings()`` without re-parsing the environment.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET = "dev-only-insecure-secret-key-change-me"
DEV_PEPPER = "dev-only-insecure-worker-pepper-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False)

    # --- Core ---------------------------------------------------------------
    environment: str = "development"
    log_level: str = "INFO"
    log_format: str = "console"
    public_base_url: str = "http://127.0.0.1:8000"

    # --- Database -----------------------------------------------------------
    database_url: str = "sqlite:///./orchestrator.db"
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle_seconds: int = 1800
    sql_echo: bool = False

    # --- Secrets ------------------------------------------------------------
    secret_key: str = DEV_SECRET
    worker_secret_pepper: str = DEV_PEPPER
    secrets_encryption_key: str = ""

    # --- Auth ---------------------------------------------------------------
    access_token_ttl_minutes: int = 720
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""
    trust_proxy_headers: bool = False
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- Supabase (identity provider only) ----------------------------------
    # Supabase issues the signed session token after Google sign-in. It never
    # stores orchestration data: workflows, runs and schedules stay in the
    # application database configured above.
    supabase_url: str = ""
    supabase_anon_key: str = ""
    # Legacy shared-secret signing (HS256). Leave empty for the modern
    # asymmetric (ES256/RS256) setup, which is verified through the project JWKS.
    supabase_jwt_secret: str = ""
    supabase_jwt_audience: str = "authenticated"
    supabase_jwks_url: str = ""
    supabase_jwks_cache_seconds: int = Field(default=3600, ge=60, le=86400)
    supabase_jwks_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    # Sign in with email + password remains available alongside Google.
    password_auth_enabled: bool = True

    # --- AI worker tasks ---------------------------------------------------
    ai_provider: Literal["groq", "gemini"] = "groq"
    # --- Error tracking (Sentry, optional) ----------------------------------
    # Set SENTRY_DSN to enable. The SDK is optional: the app boots fine
    # without it installed, and Sentry stays off when the DSN is empty.
    sentry_dsn: str = ""
    sentry_environment: str = ""
    sentry_traces_sample_rate: float = Field(default=0.1, ge=0.0, le=1.0)
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    max_ai_text_chars: int = Field(default=80_000, ge=1_000, le=1_000_000)
    # Provider fallback is opt-in and off by default: an unset fallback keeps the
    # primary provider's failures visible instead of silently switching vendors.
    ai_fallback_enabled: bool = False
    ai_fallback_provider: Literal["", "groq", "gemini"] = ""
    ai_extract_repair_attempts: int = Field(default=1, ge=0, le=3)
    ai_chunk_chars: int = Field(default=12_000, ge=500, le=100_000)
    ai_chunk_overlap_chars: int = Field(default=300, ge=0, le=5_000)
    # Cost per one million tokens: {"<model>": {"prompt": 0.15, "completion": 0.6}}.
    ai_model_prices: dict[str, dict[str, float]] = Field(default_factory=dict)
    ai_classify_min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    # --- Connectors ---------------------------------------------------------
    # Empty allow-list means "any public host"; adding names restricts outbound
    # HTTP to those hosts and their subdomains.
    connector_http_allowed_domains: list[str] = Field(default_factory=list)
    connector_http_timeout_seconds: int = Field(default=30, ge=1, le=300)
    connector_http_max_redirects: int = Field(default=3, ge=0, le=10)
    connector_http_max_response_bytes: int = Field(default=5_242_880, ge=1024, le=104_857_600)
    connector_http_max_request_bytes: int = Field(default=1_048_576, ge=1024, le=52_428_800)
    connector_http_max_text_chars: int = Field(default=200_000, ge=1_000, le=10_000_000)
    # Extra CIDR ranges to block beyond the built-in private/loopback/link-local
    # sets, e.g. ["198.18.0.0/15"].
    connector_blocked_networks: list[str] = Field(default_factory=list)
    connector_allow_private_networks: bool = False
    connector_sql_timeout_seconds: int = Field(default=30, ge=1, le=600)
    connector_sql_max_rows: int = Field(default=1_000, ge=1, le=100_000)
    # Admin opt-in: when False (default), sql.query rejects every non-SELECT
    # statement regardless of what the workflow asks for.
    connector_sql_allow_writes: bool = False
    # Dry-run mode for outbound side-effect connectors (email/slack/webhook):
    # inputs are validated and logged but nothing leaves the machine. Tests
    # enable this so suites never touch real networks.
    connectors_dry_run: bool = False
    connector_storage_max_bytes: int = Field(default=10_485_760, ge=1024, le=104_857_600)
    connector_slack_allowed_hosts: list[str] = Field(default_factory=lambda: ["hooks.slack.com"])
    connector_slack_timeout_seconds: int = Field(default=10, ge=1, le=120)
    connector_email_recipient_limit: int = Field(default=50, ge=1, le=500)
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65_535)
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    smtp_timeout_seconds: int = Field(default=15, ge=1, le=120)

    # --- Execution ----------------------------------------------------------
    lease_seconds: int = Field(default=30, ge=5, le=3600)
    worker_stale_seconds: int = Field(default=120, ge=10)
    dispatch_batch_size: int = Field(default=50, ge=1, le=1000)
    max_steps_per_run: int = Field(default=200, ge=1, le=5000)
    max_subworkflow_depth: int = Field(default=8, ge=1, le=32)
    max_workflow_steps: int = Field(default=100, ge=1, le=1000)
    max_workflow_versions: int = Field(default=200, ge=1)
    max_request_bytes: int = Field(default=52_428_800, ge=1024)
    # Maximum nesting depth for JSON request bodies; deeper payloads are
    # rejected with 413 to blunt stack-exhaustion attacks.
    max_json_depth: int = Field(default=32, ge=4, le=256)
    max_run_input_bytes: int = Field(default=262_144, ge=1024)
    max_task_output_bytes: int = Field(default=52_428_800, ge=1024)
    max_inline_output_bytes: int = Field(default=1_048_576, ge=1024)
    artifact_storage: Literal["local", "s3"] = "local"
    artifact_dir: str = "./artifacts"
    artifact_s3_bucket: str = ""
    artifact_s3_endpoint_url: str = ""
    artifact_s3_region: str = "us-east-1"
    artifact_s3_access_key_id: str = ""
    artifact_s3_secret_access_key: str = ""
    max_document_bytes: int = Field(default=5_242_880, ge=1024, le=10_485_760)
    retry_max_delay_seconds: int = Field(default=3600, ge=1)
    retry_jitter: float = Field(default=0.25, ge=0.0, le=1.0)
    # Zero disables a cap. Queue limits and task rate limits are JSON maps,
    # e.g. {"gpu": 8} and {"http.request": 120}.
    max_global_running_tasks: int = Field(default=0, ge=0)
    max_running_tasks_per_owner: int = Field(default=0, ge=0)
    queue_concurrency_limits: dict[str, int] = Field(default_factory=dict)
    task_rate_limits: dict[str, int] = Field(default_factory=dict)
    circuit_breaker_failure_threshold: int = Field(default=5, ge=0, le=1000)
    circuit_breaker_open_seconds: int = Field(default=60, ge=1, le=86400)
    poison_task_expiry_limit: int = Field(default=3, ge=1, le=20)

    # --- External triggers -------------------------------------------------
    webhook_max_clock_skew_seconds: int = Field(default=300, ge=30, le=3600)
    trigger_default_rate_limit_per_minute: int = Field(default=60, ge=1, le=10000)
    # General per-IP API rate limit (0 = disabled). Production: rate limiting.
    api_rate_limit_per_minute: int = Field(default=0, ge=0, le=100000)

    # --- Scheduler ----------------------------------------------------------
    scheduler_enabled: bool = True
    scheduler_interval_seconds: int = Field(default=15, ge=1, le=3600)
    scheduler_batch_size: int = Field(default=25, ge=1, le=500)
    scheduler_lock_seconds: int = Field(default=30, ge=5)

    # --- Outbox / Redis -----------------------------------------------------
    redis_url: str = ""
    outbox_stream: str = "orchestrator.dispatch"
    outbox_consumer_group: str = "orchestrator-workers"
    outbox_relay_interval_seconds: float = 1.0
    outbox_max_attempts: int = Field(default=10, ge=1)

    # --- Observability ------------------------------------------------------
    metrics_enabled: bool = True
    slow_query_ms: int = 500
    # Default time window (hours) the dashboard aggregates over; the API and UI
    # let callers pick 1..720 explicitly.
    default_dashboard_window_hours: int = Field(default=24, ge=1, le=720)
    # --- Quotas ---------------------------------------------------------------
    # Runs a single user may start per rolling 24h; 0 disables the check.
    quota_runs_per_day: int = Field(default=1000, ge=0)
    # Runs a single user may have active (queued/running/cancelling); 0 disables.
    quota_concurrent_runs: int = Field(default=50, ge=0)
    # Total document bytes a single user may store; 0 disables the check.
    quota_storage_bytes_per_user: int = Field(default=1_073_741_824, ge=0)

    # --- Worker client defaults --------------------------------------------
    orchestrator_api: str = "http://127.0.0.1:8000"
    worker_id: str = "sample-worker-1"
    worker_token: str = ""
    worker_task_types: str = ""
    worker_poll_seconds: float = 1.0
    worker_long_poll_seconds: int = Field(default=20, ge=0, le=25)
    worker_queues: str = "default"
    worker_heartbeat_interval: int = Field(default=10, ge=1)
    worker_max_concurrency: int = Field(default=4, ge=1, le=64)
    worker_timeout_grace_seconds: int = Field(default=5, ge=0, le=300)

    # --- Embedded worker (Stage B) -------------------------------------------
    # Runs an in-process worker inside the API process so runs execute without a
    # separate worker terminal. ``None`` (default) means: on in development/local,
    # off otherwise.
    embedded_worker_enabled: bool | None = None
    embedded_worker_concurrency: int = Field(default=4, ge=1, le=32)
    embedded_worker_queues: str = "default"
    # How long a ready step may sit queued before the UI warns that no worker
    # is available for its queue/task type.
    queue_wait_warning_seconds: int = Field(default=30, ge=5, le=3600)

    # ------------------------------------------------------------------ utils
    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod", "staging"}

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def worker_task_type_list(self) -> list[str]:
        return [item.strip() for item in self.worker_task_types.split(",") if item.strip()]

    @property
    def embedded_worker_on(self) -> bool:
        if self.embedded_worker_enabled is not None:
            return self.embedded_worker_enabled
        return self.environment.lower() in {"development", "local"}

    @property
    def embedded_worker_queue_list(self) -> list[str]:
        return [item.strip() for item in self.embedded_worker_queues.split(",") if item.strip()]

    @property
    def worker_queue_list(self) -> list[str]:
        return [item.strip() for item in self.worker_queues.split(",") if item.strip()]

    @property
    def api_base(self) -> str:
        return self.orchestrator_api.rstrip("/")

    @property
    def supabase_base_url(self) -> str:
        return self.supabase_url.rstrip("/")

    @property
    def supabase_auth_enabled(self) -> bool:
        """True when Supabase-issued tokens can be accepted."""
        return bool(self.supabase_base_url)

    @property
    def supabase_issuer(self) -> str:
        return f"{self.supabase_base_url}/auth/v1"

    @property
    def supabase_jwks_url_resolved(self) -> str:
        if self.supabase_jwks_url:
            return self.supabase_jwks_url.strip()
        return f"{self.supabase_issuer}/.well-known/jwks.json"

    @property
    def supabase_redirect_origins(self) -> list[str]:
        """Origins allowed to receive the OAuth redirect back from Supabase."""
        return self.cors_origin_list

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.upper()

    @field_validator("database_url")
    @classmethod
    def _require_driver(cls, value: str) -> str:
        if value.startswith("postgres://"):
            # Heroku-style URLs are not understood by SQLAlchemy 2.x.
            return value.replace("postgres://", "postgresql+psycopg2://", 1)
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg2://", 1)
        return value

    @field_validator("supabase_url")
    @classmethod
    def _validate_supabase_url(cls, value: str) -> str:
        """Catch Supabase project-URL typos at startup, not as login loops.

        A single wrong character in the project ref (e.g. an extra ``a`` in
        ``fdbhmmnucqbadayernurg``) makes the backend derive the wrong issuer
        and JWKS URL, so every Google sign-in token is rejected with
        ``invalid_issuer`` and the frontend bounces between login and logout.
        Supabase project refs are always exactly 20 lowercase alphanumeric
        characters, so anything else is a typo.
        """
        url = (value or "").strip().rstrip("/")
        if not url:
            return value  # Supabase sign-in disabled; nothing to check.
        if not url.startswith("https://"):
            raise ValueError("SUPABASE_URL must start with https://")
        if not url.endswith(".supabase.co"):
            raise ValueError("SUPABASE_URL must end with .supabase.co")
        ref = url[len("https://") : -len(".supabase.co")]
        if len(ref) != 20 or not ref.isalnum() or not ref.islower():
            raise ValueError(
                f"SUPABASE_URL project ref {ref!r} is invalid: Supabase project "
                "refs are exactly 20 lowercase alphanumeric characters. "
                "Check for typos (e.g. a duplicated letter making it 21 chars)."
            )
        return url

    @model_validator(mode="before")
    @classmethod
    def _read_secret_files(cls, data: Any) -> Any:
        """Support Docker-secrets style *_FILE vars (production: secrets manager).

        For each of SECRET_KEY, WORKER_SECRET_PEPPER, SECRETS_ENCRYPTION_KEY,
        GROQ_API_KEY, GEMINI_API_KEY and DATABASE_URL, a <NAME>_FILE env var
        may point to a file whose contents are used instead. Priority:
        explicit environment variable > <NAME>_FILE > .env file > default.
        """
        if isinstance(data, dict):
            for name in (
                "SECRET_KEY",
                "WORKER_SECRET_PEPPER",
                "SECRETS_ENCRYPTION_KEY",
                "GROQ_API_KEY",
                "GEMINI_API_KEY",
                "DATABASE_URL",
            ):
                if name in os.environ:
                    continue  # Explicit env var wins.
                file_var = f"{name}_FILE"
                path = os.environ.get(file_var, "").strip()
                if path:
                    try:
                        data[name.lower()] = Path(path).read_text(encoding="utf-8").strip()
                    except OSError as exc:
                        raise ValueError(f"{file_var} points to an unreadable file: {path}") from exc
        return data

    @model_validator(mode="after")
    def _guard_production_secrets(self) -> "Settings":
        if self.worker_heartbeat_interval >= self.lease_seconds:
            raise ValueError("WORKER_HEARTBEAT_INTERVAL must be less than LEASE_SECONDS")
        if self.is_production:
            problems: list[str] = []
            if self.secret_key == DEV_SECRET or len(self.secret_key) < 32:
                problems.append("SECRET_KEY must be set to a random value of at least 32 characters")
            if self.worker_secret_pepper == DEV_PEPPER or len(self.worker_secret_pepper) < 16:
                problems.append("WORKER_SECRET_PEPPER must be set to a random value of at least 16 characters")
            if not self.secrets_encryption_key:
                problems.append("SECRETS_ENCRYPTION_KEY must be set so workflow secrets can be encrypted")
            if self.artifact_storage == "s3" and not self.artifact_s3_bucket:
                problems.append("ARTIFACT_S3_BUCKET must be set when ARTIFACT_STORAGE=s3")
            # Production hardening (Issue 6): AI provider keys must be present.
            if self.ai_provider == "groq" and not self.groq_api_key.strip():
                problems.append("GROQ_API_KEY must be set when AI_PROVIDER=groq in production (rotate at https://console.groq.com/keys)")
            if self.ai_provider == "gemini" and not self.gemini_api_key.strip():
                problems.append("GEMINI_API_KEY must be set when AI_PROVIDER=gemini in production (rotate at https://aistudio.google.com/app/apikey)")
            if problems:
                raise ValueError("Unsafe production configuration: " + "; ".join(problems))
        return self

    @model_validator(mode="after")
    def _validate_artifact_storage(self) -> "Settings":
        if self.artifact_storage == "s3" and not self.artifact_s3_bucket:
            raise ValueError("ARTIFACT_S3_BUCKET must be set when ARTIFACT_STORAGE=s3")
        return self

    @model_validator(mode="after")
    def _validate_connector_settings(self) -> "Settings":
        for domain in self.connector_http_allowed_domains:
            if not domain or any(character in domain for character in " /:@") or domain.startswith("."):
                raise ValueError("CONNECTOR_HTTP_ALLOWED_DOMAINS must contain bare host names")
        for host in self.connector_slack_allowed_hosts:
            if not host or any(character in host for character in " /:@"):
                raise ValueError("CONNECTOR_SLACK_ALLOWED_HOSTS must contain bare host names")
        import ipaddress

        for network in self.connector_blocked_networks:
            try:
                parsed = ipaddress.ip_network(network, strict=False)
            except ValueError as exc:
                raise ValueError(f"CONNECTOR_BLOCKED_NETWORKS contains an invalid CIDR: {network}") from exc
            if parsed.prefixlen == 0:
                raise ValueError("CONNECTOR_BLOCKED_NETWORKS cannot contain 0.0.0.0/0 or ::/0")
        if self.smtp_host and not self.smtp_from:
            raise ValueError("SMTP_FROM must be set when SMTP_HOST is configured")
        for model, prices in self.ai_model_prices.items():
            if not model or not isinstance(prices, dict):
                raise ValueError("AI_MODEL_PRICES must map model names to {'prompt': x, 'completion': y}")
            for key, value in prices.items():
                if key not in {"prompt", "completion"} or not isinstance(value, (int, float)) or value < 0:
                    raise ValueError("AI_MODEL_PRICES entries accept non-negative 'prompt'/'completion' numbers")
        return self

    @model_validator(mode="after")
    def _validate_dispatch_limits(self) -> "Settings":
        for queue, limit in self.queue_concurrency_limits.items():
            if not queue or len(queue) > 120 or not isinstance(limit, int) or limit < 1:
                raise ValueError("QUEUE_CONCURRENCY_LIMITS must map queue names to positive integer limits")
        for task_type, limit in self.task_rate_limits.items():
            if not task_type or not isinstance(limit, int) or limit < 1:
                raise ValueError("TASK_RATE_LIMITS must map task types to positive integer limits")
        return self

    def as_public_dict(self) -> dict[str, Any]:
        """Settings that are safe to expose through the ops API."""
        return {
            "environment": self.environment,
            "database": "sqlite" if self.is_sqlite else "postgresql",
            "dispatch_backend": "redis-outbox" if self.redis_url else "database-polling",
            "worker_long_poll_seconds": self.worker_long_poll_seconds,
            "max_global_running_tasks": self.max_global_running_tasks,
            "max_running_tasks_per_owner": self.max_running_tasks_per_owner,
            "lease_seconds": self.lease_seconds,
            "scheduler_enabled": self.scheduler_enabled,
            "scheduler_interval_seconds": self.scheduler_interval_seconds,
            "max_workflow_steps": self.max_workflow_steps,
            "max_steps_per_run": self.max_steps_per_run,
            "max_run_input_bytes": self.max_run_input_bytes,
            "max_task_output_bytes": self.max_task_output_bytes,
            "max_document_bytes": self.max_document_bytes,
            "metrics_enabled": self.metrics_enabled,
            "auth": {
                "password": self.password_auth_enabled,
                "google": self.supabase_auth_enabled,
                "token_verification": self.supabase_verification_mode,
            },
        }

    @property
    def supabase_verification_mode(self) -> str:
        """Which verification path is configured (never a secret value)."""
        if not self.supabase_auth_enabled:
            return "disabled"
        if self.supabase_jwt_secret:
            return "shared-secret"
        return "jwks"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test helper: re-read the environment on the next ``get_settings()``."""
    get_settings.cache_clear()


settings = get_settings()

# Convenience aliases used across the codebase.
LEASE_SECONDS = settings.lease_seconds
MAX_WORKFLOW_STEPS = settings.max_workflow_steps

__all__ = ["Settings", "get_settings", "reset_settings_cache", "settings", "LEASE_SECONDS", "MAX_WORKFLOW_STEPS"]


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
