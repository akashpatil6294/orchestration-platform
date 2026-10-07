# SETUP_NOTES.md — dev vs prod modes (production hardening)

## Three modes

| Mode | Command | Backend | Frontend | Database | Worker |
|------|---------|---------|----------|----------|--------|
| `dev` | `make dev` or `python -m app.cli dev` | 127.0.0.1:8001 | localhost:5173 | SQLite `./orchestrator.db` | embedded in API |
| `dev-lan` | `make dev-lan` | 0.0.0.0:8001 | 0.0.0.0:5173 | SQLite | embedded in API |
| `prod` | see below | 127.0.0.1:8001 behind Caddy | Caddy static + HTTPS | Postgres | `scripts/run_worker.py` (×N) |

`dev` is the default. `dev-lan` exposes the LAN demo to your phone.
`prod` is the only internet-safe mode.

## Environment variables per mode

### dev / dev-lan (defaults work)
No `.env` required for a first boot. Copy `.env.example` to `.env` when you
need to change something. Key dev values:
- `ENVIRONMENT=development`
- `DATABASE_URL=sqlite:///./orchestrator.db`
- `CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173`
- `VITE_API_BASE_URL=http://127.0.0.1:8001` (in `frontend/.env.local`)

### prod (all REQUIRED)
- `ENVIRONMENT=production`
- `SECRET_KEY` — 32+ random chars: `python -m app.cli gen-secret --bytes 32`
- `WORKER_SECRET_PEPPER` — 16+ random chars
- `SECRETS_ENCRYPTION_KEY` — 32+ random chars (or Fernet key)
- `DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/db`
- `CORS_ORIGINS=https://app.<your-domain>`
- `GROQ_API_KEY` or `GEMINI_API_KEY` (whichever `AI_PROVIDER` selects)
- `EMBEDDED_WORKER_ENABLED=false`
- Frontend: build with `frontend/.env.production` (see
  `frontend/.env.production.example`); `VITE_API_BASE_URL` empty for
  same-origin via Caddy.

## Are we prod-safe? (two commands)

```powershell
# 1. Strict gate — exits 1 with a FAIL table if anything is unsafe
python -m app.cli doctor --strict

# 2. Watch the startup log — it prints environment, secret posture
#    ("present" vs "DEV DEFAULTS"), database backend, CORS origins,
#    and embedded worker state. Never prints values.
```

## Production start order

```powershell
# 1. Migrate
alembic upgrade head              # or: make migrate

# 2. API (repeatable behind Caddy; Caddy terminates TLS)
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001

# 3. Workers (one per terminal, or a process manager; scale horizontally)
python scripts/run_worker.py       # or: make worker

# 4. Frontend is static: `npm run build` in frontend/, served by Caddy
# 5. Reverse proxy
caddy run --config Caddyfile       # or: make serve
```

## When something breaks

Check the backend log first. The app logs these at startup:
- `Startup configuration` — environment, secret posture, database, CORS
  origins, embedded worker state
- `Supabase issuer` / `JWKS URL` — the exact issuer and JWKS endpoint in use
- `Rejected a Supabase session token ... reason=<code>` — auth failures;
  `jwks_unavailable` means the Supabase URL is wrong (JWKS fetch runs
  before the issuer check), `invalid_issuer`/`invalid_audience` mean
  config drift, `token_expired` means clock skew
- `CORS origin uses http:// in production` — fix `CORS_ORIGINS`
- `SQLite in production` — migrate to Postgres via
  `scripts/migrate_sqlite_to_postgres.py`

Then run `python -m app.cli doctor` (informational) or
`python -m app.cli doctor --strict` (gate).

## Production operations

### Backups

```powershell
# Daily backup (Postgres only; SQLite needs no pg_dump — copy the .db file)
python scripts/backup_postgres.py --keep 14

# Restore (asks for confirmation)
python scripts/restore_postgres.py ./backups/orchestrator-20261007T020000Z.dump
```

Schedule `backup_postgres.py` daily via cron or Windows Task Scheduler.
Test restores quarterly — an untested backup is not a backup.

### Log aggregation

Set `LOG_FORMAT=json` in production. Every line is JSON, redacted (secret
values never reach the logs), and carries `request_id`. Ship stdout to
your aggregator:

- **Loki**: promtail `static_configs` scraping the API's stdout
- **Datadog**: `datadog-agent` with `logs_enabled: true` + JSON parsing
- **CloudWatch**: awslogs driver with the `json` filter pattern

### Error tracking (Sentry)

```powershell
pip install sentry-sdk          # optional; app boots fine without it
# .env:
# SENTRY_DSN=https://<key>@<org>.ingest.sentry.io/<project>
# SENTRY_ENVIRONMENT=production   # defaults to ENVIRONMENT
```

Sentry stays off when `SENTRY_DSN` is empty. 10% trace sampling by
default (`SENTRY_TRACES_SAMPLE_RATE`).

### Rate limiting

Auth endpoints are rate-limited per IP (30/min) out of the box. For a
general per-IP API limit in production:

```
API_RATE_LIMIT_PER_MINUTE=600
```

`/health` and `/ready` are exempt so load-balancer probes never trip it.
For multi-instance deployments, replace the in-memory limiter with Redis.

### Secrets

Beyond `.env`, the app reads Docker-secrets style `*_FILE` vars:

```
SECRET_KEY_FILE=/run/secrets/secret_key
DATABASE_URL_FILE=/run/secrets/database_url
```

Supported: `SECRET_KEY`, `WORKER_SECRET_PEPPER`, `SECRETS_ENCRYPTION_KEY`,
`GROQ_API_KEY`, `GEMINI_API_KEY`, `DATABASE_URL`. Explicit values win over
`_FILE`. For Vault/AWS Secrets Manager, render secrets to files at deploy
time and point the `*_FILE` vars at them.

### Zero-downtime deploys

1. Run two API processes (ports 8001, 8002), each writing its PID to
   `/run/orchestrator/api-<port>.pid`.
2. Uncomment the multi-backend block in `Caddyfile` (Caddy health-checks
   `/health` and routes around a restarting instance).
3. Deploy with:

```powershell
python scripts/rolling_restart.py --pids /run/orchestrator/api-8001.pid /run/orchestrator/api-8002.pid --start-cmd "python -m uvicorn app.main:app --host 127.0.0.1 --port {port}"
```

The script stops one process, starts its replacement, waits for `/health`,
then moves to the next. It aborts if a replacement never becomes healthy.

### Monitoring / alerting

```powershell
# Exit 0 = healthy, 1 = down (cron/Task Scheduler friendly)
python scripts/healthcheck.py
```

Pair with an external alerter (UptimeRobot, Better Stack, PagerDuty):
probe `/health` every minute and alert on non-200. The script also checks
`/ready` component status.

### Cost controls

Two layers:

1. **In-app** (already built): workflow budgets with warn (80%) and
   hard-stop (100%) thresholds, plus `cost_exceeds` alert rules — manage
   at `/alerts`.
2. **Cloud**: set a spend cap where you host (AWS Budgets, GCP budgets,
   Render spend limits). `doctor --strict` prints a reminder; it cannot
   enforce infra-level caps.
