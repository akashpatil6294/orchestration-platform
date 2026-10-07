# Workflow Orchestration Platform

A self-hosted platform for building, running and observing versioned workflows.

Workflows are directed acyclic graphs. You edit a draft, validate it, and publish
an immutable version. A run is pinned to a published version and its steps are
executed by independent workers: dependencies decide readiness, retries use
exponential backoff, every step has a timeout, and claims are held under an
expiring lease. Runs, steps, attempts, events, schedules, workers and leases are
all durable rows in one database.

**Sign in with Google.** Authentication is delegated to Supabase Auth; the
platform keeps its own user accounts and enforces ownership on every query.

---

## Contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [1. Install the backend](#1-install-the-backend)
- [2. Create the backend `.env`](#2-create-the-backend-env)
- [3. Configure Supabase](#3-configure-supabase)
- [4. Configure Google OAuth](#4-configure-google-oauth)
- [5. Configure the frontend](#5-configure-the-frontend)
- [6. Run the database migrations](#6-run-the-database-migrations)
- [7. Start FastAPI](#7-start-fastapi)
- [8. Start Vite](#8-start-vite)
- [9. Open the frontend and sign in](#9-open-the-frontend-and-sign-in)
- [Expected login flow](#expected-login-flow)
- [How authentication works](#how-authentication-works)
- [Running the tests](#running-the-tests)
- [The sample workflow](#the-sample-workflow)
- [Environment variables](#environment-variables)
- [Migrations](#migrations)
- [Troubleshooting sign-in](#troubleshooting-sign-in)
- [Deploying to production](#deploying-to-production)

---

## Architecture

```
Browser (React + Vite, http://localhost:5173)
    |  1. "Continue with Google"
    v
Supabase Auth  ---- 2. Google OAuth ---->  Google
    |  3. session token (ES256-signed JWT)
    v
Browser
    |  4. Authorization: Bearer <Supabase token>
    v
FastAPI (http://127.0.0.1:8000)
    |  5. verify signature against the project JWKS
    |  6. map the Supabase subject onto an application user
    v
Database (PostgreSQL or SQLite)
    users, workflows, workflow_versions, runs, step_runs, step_attempts,
    run_events, schedules, workers, outbox_messages, secrets
```

### Where Supabase stops

Supabase is used for **authentication only**.

- It never stores orchestration data. Workflows, versions, runs, steps,
  attempts, events, schedules, workers and leases live in the database named by
  `DATABASE_URL` and are reached through the existing SQLAlchemy engine.
- No Supabase database client, no `service_role` key and no PostgREST call
  exists anywhere in this codebase.
- The browser only ever receives the project URL and the publishable (anon) key.

### Backend layout

| Path | Responsibility |
| --- | --- |
| `app/main.py` | App factory, middleware, router registration, lifespan |
| `app/config.py` | All settings, environment-driven |
| `app/database.py` | The single engine, session factory and readiness probe |
| `app/models/` | SQLAlchemy models for every table |
| `app/schemas/` | Request/response models |
| `app/api/` | Route modules |
| `app/services/` | Business logic: workflows, runs, workers, schedules, events, outbox |
| `app/core/auth.py` | The one place a bearer token becomes an authenticated user or worker |
| `app/auth/` | Supabase token verification and application-user mapping |
| `app/worker/` | Task handler allow-list, registry and runtime |
| `app/scheduler.py`, `app/sample_worker.py` | Background processes |
| `migrations/` | Alembic revisions |
| `frontend/` | React, TypeScript, Vite single-page application |

---

## Prerequisites

- **Python 3.11+**
- **Node.js 20+** (developed against Node 24)
- A PostgreSQL database, or nothing at all if you prefer the SQLite default
- A Supabase project with the Google provider enabled (steps 3 and 4)

---

## 1. Install the backend

```bash
cd "orchestration platform"
python -m venv .venv
.venv/Scripts/Activate.ps1          # Windows PowerShell
# source .venv/bin/activate         # macOS / Linux
pip install -r requirements.txt
```

---

## 2. Create the backend `.env`

```bash
cp .env.example .env
```

Then set, at minimum:

| Variable | Why |
| --- | --- |
| `DATABASE_URL` | Where the platform stores everything. PostgreSQL or SQLite. |
| `SECRET_KEY` | Signs this application's own session tokens. Generate with `python -m app.cli gen-secret`. |
| `WORKER_SECRET_PEPPER` | Peppers worker and API token hashes. |
| `SECRETS_ENCRYPTION_KEY` | Encrypts workflow secret values. |
| `SUPABASE_URL` | Enables Google sign-in. |
| `CORS_ORIGINS` | Must include the frontend origin. Defaults to `http://localhost:5173,http://127.0.0.1:5173`. |

`SUPABASE_JWT_SECRET` is **optional**: set it only if your project still signs
session tokens with the legacy HS256 shared secret. Projects using asymmetric
signing keys (the current Supabase default, and what this deployment uses) are
verified through the project's public JWKS. The doctor command below reports
which mode is active.

Check the configuration at any time:

```bash
python -m app.cli doctor
```

```
Database      : postgresql+psycopg2 (reachable)
Environment   : development
Secret key    : customised
Sign-in       : email + password + Google via Supabase (jwks)
Supabase      : https://fdbhmmnucqbdayernurg.supabase.co
  issuer      : https://fdbhmmnucqbdayernurg.supabase.co/auth/v1
  anon key    : not set
  shared secret: not set
Accounts      : 0
Workers       : 0
```

No secret value is ever printed — only whether one is configured.

---

## 3. Configure Supabase

In the Supabase dashboard for project `fdbhmmnucqbdayernurg`:

1. **Authentication → Sign In / Providers → Google**: enable it and paste the
   Google **Client ID** and **Client Secret** from step 4. The client secret
   lives here and in the Google console only — never in this repository and
   never in a `VITE_` variable.
2. **Authentication → URL Configuration**:
   - **Site URL**: `http://localhost:5173`
   - **Redirect URLs**: add both development origins
     - `http://localhost:5173`
     - `http://127.0.0.1:5173`

   Without these entries Supabase refuses to return the browser to the app.
3. **Project Settings → API**: copy the **publishable / anon** key for step 5.

The address Supabase itself is called back on after Google is:

```
https://fdbhmmnucqbdayernurg.supabase.co/auth/v1/callback
```

which is also what belongs in the Google console (step 4).

---

## 4. Configure Google OAuth

1. In the [Google Cloud console](https://console.cloud.google.com/apis/credentials),
   create an **OAuth client ID** of type **Web application**.
2. **Authorised JavaScript origins**
   - `https://fdbhmmnucqbdayernurg.supabase.co`
3. **Authorised redirect URIs**
   - `https://fdbhmmnucqbdayernurg.supabase.co/auth/v1/callback`
4. Copy the generated **Client ID** and **Client Secret** straight into the
   Supabase Google provider (step 3). This application never reads them.
5. While the OAuth consent screen is unpublished, add your own Google account
   under **Test users**.

---

## 5. Configure the frontend

```bash
cd frontend
npm install
cp .env.example .env.local
```

`frontend/.env.local`:

```ini
VITE_SUPABASE_URL=https://fdbhmmnucqbdayernurg.supabase.co
VITE_SUPABASE_ANON_KEY=<the publishable/anon key from step 3>
VITE_API_BASE_URL=http://127.0.0.1:8000
```

Only these three variables exist, and all three are public. Vite compiles
everything prefixed with `VITE_` into the browser bundle, so the `service_role`
key, the Google client secret, the database password and every `SECRET_KEY`-style
value stay out of this file. `frontend/.env.local` is git-ignored.

---

## 6. Run the database migrations

```bash
python -m app.cli migrate
```

This applies the initial schema plus the Supabase identity mapping
(`users.supabase_user_id`, a nullable `users.password_hash`, `users.auth_provider`
and `users.avatar_url`). It is safe to re-run and works on both PostgreSQL and
SQLite.

---

## 7. Start FastAPI

One command does everything for local development — migrations, demo seeding
when the database is empty, and the API with the embedded worker and scheduler:

```bash
# from the project root, with the virtual environment active
python -m app.cli dev
# Windows PowerShell: .\scripts\dev.ps1
```

- API: <http://127.0.0.1:8000>
- Interactive docs: <http://127.0.0.1:8000/docs>
- Health: <http://127.0.0.1:8000/health>, readiness: <http://127.0.0.1:8000/ready>

The embedded worker runs inside the API process (on by default when
`ENVIRONMENT` is `development` or `local`, off otherwise), so runs execute with
no second terminal. It registers as `embedded-<hostname>-<pid>-<rand>`, uses the
same claim path, leases, heartbeats and events as an external worker, and its
credential is generated at startup, held in memory, never logged. Tune it with
`EMBEDDED_WORKER_ENABLED`, `EMBEDDED_WORKER_CONCURRENCY` (1–32, default 4) and
`EMBEDDED_WORKER_QUEUES` (default `default`).

Prefer the classic two-terminal setup? Disable the embedded worker and start one manually:

```bash
uvicorn app.main:app --reload
# second terminal:
python -m app.cli create-worker --worker-id local-1 --task-types demo.echo,demo.add,demo.fail_once,demo.sleep,demo.summarize
python -m app.sample_worker
```

When a run sits queued with no worker to run it, the dashboard and run detail
pages show a banner naming the queue/task type, with a **Start embedded worker**
button (admins, when the setting allows), a **Generate worker token** button
(admins; mints a credential via `POST /api/v1/workers` and shows the plaintext
token exactly once, prefilled into the copied start command), and a copyable
worker command. `GET /api/v1/ops/capacity` exposes the same data: active
workers, queued steps per queue, oldest queued age, and per task type whether
any active worker covers it — queue/task-type aggregates are scoped to the
requesting owner for non-admins, so tenants never see each other's demand.
Each step also reports a `wait_reason` (`waiting_for_dependencies`,
`waiting_for_worker`, `rate_limited`, `concurrency_limited`, `circuit_open`,
`approval_pending`, `paused`).

An on-demand embedded worker can also be managed via the API:
`POST /api/v1/ops/embedded-worker/start` (idempotent), `GET` the same path for
status, and `POST /api/v1/ops/embedded-worker/stop` for a graceful drain.
It is stopped automatically on API shutdown.

While a task is executing, the worker renews its lease every
`WORKER_HEARTBEAT_INTERVAL` seconds (default `10`). Keep this interval below
`LEASE_SECONDS` (default `30`); settings reject an interval that is too long.

AI summary and classification tasks use Groq by default. Set `GROQ_API_KEY` in
the backend/worker `.env`; the default model is `openai/gpt-oss-120b`. To use
Gemini instead, set `AI_PROVIDER=gemini` and configure `GEMINI_API_KEY`.

---

## 8. Start Vite

```bash
cd frontend
npm run dev
```

Frontend: <http://localhost:5173>

---

## 9. Open the frontend and sign in

1. Open <http://localhost:5173>. Any protected page redirects to `/login`.
2. Click **Continue with Google**.
3. Choose your Google account and consent.
4. Supabase returns the browser to the app with a session.
5. The app lands on `/dashboard`. The first sign-in creates exactly one row in
   `users` with `supabase_user_id`, `auth_provider = 'google'`, no password, and
   your Google name, address and picture.

Confirm the account exists:

```bash
python -m app.cli doctor          # "Accounts: 1"
```

---

## Expected login flow

```
React frontend
   "Continue with Google"  ->  supabase.auth.signInWithOAuth({ provider: "google", ... })
        |
        v
Supabase Auth  ->  Google OAuth consent  ->  Supabase session (signed JWT)
        |
        v
React restores the session and follows auth state changes
        |
        v
Every API call sends  Authorization: Bearer <Supabase access token>
        |
        v
FastAPI verifies the token, maps it onto an application user, and serves only
that user's workflows, runs and schedules
```

Nothing in the browser decides authorisation. Hiding a route is a convenience;
the API re-derives identity from the verified token on every request.

---

## How authentication works

### Verifying the token

`app/auth/supabase.py` accepts a Supabase access token and rejects anything it
cannot fully trust:

| Check | Behaviour |
| --- | --- |
| Algorithm | `ES256`, `RS256` and (legacy) `HS256` only; `alg: none` is refused |
| Signature | Asymmetric tokens are checked against the project JWKS, cached in memory and refreshed once on key rotation. `HS256` uses `SUPABASE_JWT_SECRET`. |
| Issuer | Must equal `<SUPABASE_URL>/auth/v1` |
| Audience | Must equal `SUPABASE_JWT_AUDIENCE` (default `authenticated`) |
| Expiry | Enforced, with 30 seconds of tolerated clock skew |
| Subject | Required; anonymous Supabase sessions are refused |

The shared secret is never considered for a token that advertises a different
algorithm, so a token cannot be downgraded to a symmetric check. Failures produce
a `401` with a stable reason code such as `invalid_signature` or `token_expired`;
the token itself is never logged.

### Mapping the identity onto an application user

`app/auth/service.py` resolves the verified subject in this order:

1. **`users.supabase_user_id = <sub>`** — the normal path and the durable
   identity key. A changed Google address still resolves to the same account.
2. **An existing password-less account with the same address** — adopted, so
   older provider accounts are not duplicated.
3. **Create** — a new account with `password_hash = NULL`.

An account that has a password is never adopted automatically. Its email was
never verified by this platform, so linking on an address match alone would let
whoever registered the address first inherit somebody else's Google identity.
Those sign-ins receive a clear `409 email_in_use` message instead. A second
Supabase identity claiming an address that is already linked is refused for the
same reason.

### One entry point for every route

`app/core/auth.py::current_user` accepts three credential types and works out
which one it was given:

- an application session token from `POST /api/v1/auth/login`,
- a Supabase session token (Google sign-in),
- a long-lived API token from `POST /api/v1/auth/tokens`.

Because this is the same dependency every protected route already used, the
workflow, run, schedule, worker and ops endpoints enforce ownership without any
change to their own code. Workers keep a separate credential: worker routes never
accept user credentials, and user routes never accept worker credentials.

### Endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /api/v1/auth/register` | Email + password account (the first account becomes admin) |
| `POST /api/v1/auth/login` | Application session token |
| `GET /api/v1/auth/session` | Profile, permissions and `sign_in_method` |
| `POST` / `GET` / `DELETE /api/v1/auth/tokens` | Long-lived API tokens for scripts |
| `POST /api/v1/auth/worker-tokens` | Mint a worker credential |

Google sign-in happens entirely between the browser and Supabase; this API never
sees a password and never holds an OAuth client secret.

---

## Running the tests

Backend — 275 tests. No network access and no real Google login is required:
JWTs are minted locally, and the asymmetric path is exercised by serving a
generated public key in place of the live JWKS.

```bash
pytest                                   # or: python -m pytest
pytest tests/test_supabase_auth.py -v     # the sign-in specific suite
```

`tests/test_supabase_auth.py` covers: unauthenticated `401`; invalid signature;
expired token; wrong issuer; wrong audience; `alg: none`; anonymous sessions;
Supabase disabled; a valid identity authenticating; the first sign-in creating an
account; repeated sign-in not duplicating it; a changed address still resolving
through the subject; password accounts not being silently linked; a second
identity being refused an already-linked address; a deactivated account; own
workflow access; cross-user workflow, run and schedule denial; session expiry and
sign-out; JWKS verification including key rotation, an unknown key and an
unreachable key set failing closed; and application and API tokens continuing to
work.

Frontend — 29 tests with Vitest and Testing Library:

```bash
cd frontend
npm test        # vitest run
npm run build   # tsc -b && vite build
npm run lint    # oxlint
```

They cover: the login page rendering and its Google action; the disabled
redirecting state; the loading state; an OAuth error displayed to the visitor; no
internal detail leaking into the page; an unauthenticated visit to a protected
route redirecting to `/login` and remembering the destination; an authenticated
visitor reaching the page; the profile name, address and avatar rendering;
sign-out; and the API client attaching the bearer token, refreshing once on a
`401`, abandoning an unrecoverable session, and surfacing backend errors.

---

## The sample workflow

```bash
python -m app.cli seed-demo --email you@example.com
```

Installs a graph demonstrating two steps running in parallel, a downstream step
consuming both outputs, and a step configured to fail once and pass after a
retry. Open it in the UI, publish it, start a run, and watch the step states,
attempt counts, outputs and logs.

Step inputs may safely reference run input and declared dependency outputs.
References use JSON paths and are never evaluated as code:

```json
{
  "input": {
    "text": "{{steps.extract.output.text}}",
    "tenant": "{{input.tenant.id}}",
    "options": "{{steps.configure.output.options}}"
  },
  "depends_on": ["extract", "configure"]
}
```

A reference to a step requires that step to appear in `depends_on`. A reference
that is syntactically valid but points to a missing runtime field fails that
step before worker dispatch, with a visible `input_reference_unavailable`
error. A reference occupying the whole value preserves its JSON type; an
embedded reference interpolates scalar values only.

Steps default to `"required": true`. A failed optional step (`"required": false`)
does not by itself fail the run; its dependents are skipped because their input
is unavailable. A skipped required dependent still fails the run.

Task types are dispatched by name to a worker's explicit handler allow-list
(`python -m app.cli task-types`). The server never evaluates code from a workflow
definition. Dispatch is at-least-once, so side-effecting handlers must be
idempotent or be driven by the run's idempotency key.

---

## Connectors, secrets and AI tasks

**Connectors** (`http.request`, `webhook.deliver`, `email.send`,
`storage.*`, `sql.query`) call external systems from a workflow step with
SSRF/DNS-rebinding protection, SQL restrictions, storage path controls,
webhook signing and dry-run support.

**Workflow secrets** are stored encrypted and referenced two ways:
`{"$secret": "NAME"}` objects or `{{secrets.NAME}}` templates inside string
values. Values are interpolated only when a worker claims the task — they
never appear in persisted rows, API responses or logs.

**AI tasks** (Groq by default; provider fallback is opt-in and off by
default):

- `ai.summarize` — concise summary of text.
- `ai.classify` — category + reason + self-assessed confidence, with
  `min_confidence` and `on_low_confidence: flag|fail` routing.
- `ai.extract` — JSON validated against `json_schema`, with one auto-repair
  retry, chunked processing for long texts and list-field merging.
- `ai.eval` — pass-rate scoring of an extraction over fixed samples.

Token usage and cost are tracked per step (`ai_usage` in step output) and
aggregated per run; the run detail page shows per-model tokens and cost from
`/api/v1/runs/{id}/stats`.

---

## Environment variables

`.env.example` is the authoritative list: every variable is labelled `PUBLIC`,
`PRIVATE`, `REQUIRED` or `OPTIONAL`, and the backend and frontend sections are
separated. The frontend also has its own template at `frontend/.env.example`.

The rule that matters:

- **Backend `.env`** — `DATABASE_URL`, `SECRET_KEY`, `WORKER_SECRET_PEPPER`,
  `SECRETS_ENCRYPTION_KEY`, `SUPABASE_URL`, optional `SUPABASE_JWT_SECRET`, and
  everything else that is not prefixed with `VITE_`.
- **`frontend/.env.local`** — `VITE_SUPABASE_URL`,
  `VITE_SUPABASE_ANON_KEY` and `VITE_API_BASE_URL` only. If a value is not safe
  to publish, it does not belong in this file.

---

## Migrations

```bash
python -m app.cli migrate                     # upgrade to head
alembic upgrade head                          # equivalent
alembic revision --autogenerate -m "message"  # after changing models
alembic downgrade -1                          # one revision back
```

`migrations/env.py` reads the URL from application settings, so migrations and
the running application can never disagree about which database they use.

---

## Troubleshooting sign-in

| Symptom | Cause |
| --- | --- |
| Sent back to `/login` in a loop | `SUPABASE_URL` is unset on the backend, so Supabase tokens are ignored. Check `python -m app.cli doctor`. |
| `Your session has expired or is invalid` right after signing in | Issuer or audience mismatch. The token's `iss` must be `<SUPABASE_URL>/auth/v1` and its `aud` must be `authenticated`. |
| Backend logs `jwks_unavailable` | The API could not reach `<SUPABASE_URL>/auth/v1/.well-known/jwks.json`. |
| Backend logs `shared_secret_missing` | The project signs with HS256 but `SUPABASE_JWT_SECRET` is empty. |
| `Access blocked: this app is not verified` | Add your Google account as a test user on the OAuth consent screen. |
| Supabase returns `redirect_uri_mismatch` | Add `http://localhost:5173` and `http://127.0.0.1:5173` under Authentication → URL Configuration. |
| A CORS error in the browser console | `CORS_ORIGINS` must list the exact frontend origin. |
| `An account with this email already exists` (`409 email_in_use`) | A password account already uses that address. Sign in with the password, or use another Google account. |

Confirm at any time that no secret reached the browser bundle:

```bash
cd frontend && npm run build && (grep -rE "service_role|client_secret|SECRET_KEY" dist/ || echo "clean")
```

---

## Deploying to production

The app runs on the local LAN by default. To expose it on the public
internet safely, follow these steps. See `SETUP_NOTES.md` for the full
dev vs prod reference.

### 1. Rotate secrets

```powershell
# Generate three independent random values (run once per environment)
python -m app.cli gen-secret
python -m app.cli gen-secret
python -m app.cli gen-secret
```

Paste the results into `.env` as `SECRET_KEY` (32+ chars),
`WORKER_SECRET_PEPPER` (16+ chars) and `SECRETS_ENCRYPTION_KEY`.
The app refuses to boot with `ENVIRONMENT=production` and dev defaults.

If the Groq or Gemini API keys were ever pasted into chat or logs, treat
them as compromised and rotate them:
- Groq: https://console.groq.com/keys
- Gemini: https://aistudio.google.com/app/apikey

### 2. Migrate SQLite to Postgres

```powershell
# Copy data to Postgres (creates schema, copies tables, validates counts)
python scripts/migrate_sqlite_to_postgres.py --sqlite ./orchestrator.db
```

Then set `DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/db` in
`.env`. SQLite is single-writer and will lock under concurrent users.

### 3. Configure production environment

Set in `.env` (see the `PRODUCTION ONLY` block in `.env.example`):

```
ENVIRONMENT=production
SECRET_KEY=<64-char-random>
WORKER_SECRET_PEPPER=<32-char-random>
SECRETS_ENCRYPTION_KEY=<32-char-random>
DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/orchestrator
CORS_ORIGINS=https://app.<your-domain>
GROQ_API_KEY=<rotated-key>        # or GEMINI_API_KEY, per AI_PROVIDER
EMBEDDED_WORKER_ENABLED=false
```

### 4. Build the frontend for production

```powershell
# Copy and fill in frontend/.env.production (VITE_API_BASE_URL stays empty
# for same-origin /api/* calls through Caddy)
cp frontend/.env.production.example frontend/.env.production
cd frontend && npm run build
```

`VITE_API_BASE_URL` in production must be empty (same-origin via Caddy)
or `https://api.<your-domain>`. Never `http://`.

### 5. Start Caddy (HTTPS)

```powershell
# Edit Caddyfile: replace <your-domain> with your actual domain
caddy run --config Caddyfile
```

Caddy gets TLS certificates from Let's Encrypt automatically and proxies
`/api/*` to FastAPI on `127.0.0.1:8001`, serving `frontend/dist` as
static files.

### 6. Start the API and workers (2+ processes)

```powershell
# Terminal 1: API
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001

# Terminal 2+: standalone workers (repeatable horizontally)
python scripts/run_worker.py
```

The embedded worker is disabled in production by default; the app raises
at startup if `EMBEDDED_WORKER_ENABLED=true` with `ENVIRONMENT=production`.

### 7. Verify

```powershell
python -m app.cli doctor --strict   # must print PASS on every check
```

Watch the startup log: it prints environment, secret posture
(`present` vs `DEV DEFAULTS`), database backend, CORS origins and worker
state — never the values themselves.

---

## Phase 1: production workflow execution

### Conditions and joins

Conditions use a small parser for comparisons, boolean operators, arithmetic and
JSON paths under `input.*` or `steps.<id>.output.*`. They never evaluate Python
or workflow-supplied code. A false `when` marks that step as an optional
condition skip. The `condition` task type provides an explicit if/else selector;
`join` can be `all_success`, `any_success` or `all_done`.

Real-world use: route an order above a risk threshold to a human review while
letting low-risk orders continue automatically. Deploy steps can also wait for
all test steps to succeed.

```json
{
  "steps": [
    {"id": "tests", "type": "ci.run", "input": {}},
    {"id": "deploy", "type": "deploy.release", "input": {}, "depends_on": ["tests"], "join": "all_success", "when": "steps.tests.output.passed == true"}
  ]
}
```

### Dynamic fan-out and fan-in

`foreach` expands one definition into indexed child step-runs after its source
output is available. `max_concurrency` bounds active children and
`partial_failure` chooses `fail_fast` or `continue`. The parent output is an
ordered list, and downstream steps can consume it after the children finish.

Real-world use: extract and classify a batch of uploaded PDFs, process each
customer record, or roll out to a bounded number of regions at a time.

```json
{
  "steps": [
    {"id": "extract", "type": "document.extract_text", "input": {"pdf_base64": "{{item.pdf_base64}}"}, "foreach": "{{input.pdfs}}", "max_concurrency": 2, "partial_failure": "continue"},
    {"id": "classify", "type": "ai.classify", "input": {"text": "{{item.text}}"}, "depends_on": ["extract"], "foreach": "{{steps.extract.output}}", "max_concurrency": 2}
  ]
}
```

### Sub-workflows

`workflow.run` accepts a published workflow id (and optional version/input),
starts it under the same owner, waits for completion and returns its output.
Parent cancellation propagates to a running child. The server rejects cycles
and nesting beyond `MAX_SUBWORKFLOW_DEPTH`.

Real-world use: share a verified onboarding, identity-check or data-cleaning
pipeline between multiple top-level workflows without copying its steps.

```json
{"steps": [{"id": "onboarding", "type": "workflow.run", "input": {"workflow": "published-workflow-id", "input": {"customer": "{{input.customer}}"}}}]}
```

### Human approval gates

An `approval` step waits for an authenticated owner or configured approver to
approve or reject it through the API or run page. Decisions and comments are
recorded in the run event history. `on_timeout` can fail, approve or reject the
gate.

Real-world use: require a reviewer to sign off on a loan, invoice or production
deployment before a side-effecting step becomes eligible.

```json
{"steps": [{"id": "review", "type": "approval", "input": {"invoice": "{{input.invoice_id}}"}, "approvers": ["finance@example.com"], "timeout_seconds": 3600, "on_timeout": "reject"}, {"id": "pay", "type": "payments.capture", "input": {}, "depends_on": ["review"]}]}
```

### Run controls, failure handlers and compensation

Definitions may set `timeout_seconds` and `sla_seconds`. Runs can be paused,
resumed, retried from a step, or cancelled. `on_failure` steps run after saga
compensations finish. A step's `compensate` field names a compensation step
that runs if later required work fails.

Real-world use: if a booking reserves a seat and a later payment fails, release
the seat before running alert or cleanup steps.

```json
{
  "timeout_seconds": 900,
  "sla_seconds": 600,
  "steps": [
    {"id": "reserve", "type": "inventory.reserve", "input": {}, "compensate": "release"},
    {"id": "charge", "type": "payments.capture", "input": {}, "depends_on": ["reserve"]},
    {"id": "release", "type": "inventory.release", "input": {}, "depends_on": ["reserve"]}
  ],
  "on_failure": [{"id": "alert", "type": "notifications.send", "input": {"message": "Booking rolled back"}}]
}
```

### Step caching and large outputs

`cache: {"ttl_seconds": ..., "key": "input-hash"}` memoizes successful,
secret-free work by task type and resolved input. Outputs over
`MAX_INLINE_OUTPUT_BYTES` are stored outside the database and shown as an
owner-scoped artifact URI. Local disk is the default; S3-compatible storage is
selected with `ARTIFACT_STORAGE=s3` and `ARTIFACT_S3_*` settings.

Real-world use: avoid repeating an expensive model or data transformation call
for identical input and pass large extraction results between worker steps
without embedding the entire result in run detail responses.

```json
{"steps": [{"id": "summarize", "type": "ai.summarize", "input": {"text": "{{input.document_text}}"}, "cache": {"ttl_seconds": 3600, "key": "input-hash"}}]}
```

## Phase 2: triggers and interval-aware schedules

### Signed webhooks

Create a trigger for a published workflow from **Triggers** in the UI or
`POST /api/v1/workflows/{workflow_id}/triggers`. A webhook trigger is scoped to
its owner and a published workflow version. The Triggers page generates the
signing key in the browser, sends it over the authenticated request, and shows
that local value once. The server encrypts it at rest; no API response or log
contains the key. Copy it into the caller's secret manager. The demo seed
command creates a webhook endpoint without printing its secret; open the
Triggers page and rotate the demo key before sending requests.

Direct API clients must include a locally generated `signing_secret` of at
least 32 characters in the create request body. Secret rotation uses the same
field at `POST /api/v1/triggers/{trigger_id}/rotate-secret`; the response
contains configuration only. `WEBHOOK_MAX_CLOCK_SKEW_SECONDS` controls the
timestamp replay window (default 300 seconds), and
`TRIGGER_DEFAULT_RATE_LIMIT_PER_MINUTE` sets the default accepted delivery
limit (default 60).

Each request must include `X-Orchestrator-Timestamp` (Unix seconds),
`X-Orchestrator-Signature`, and a unique `Idempotency-Key`. Compute the
lowercase hex HMAC-SHA256 over the timestamp, a period, and the exact raw body
bytes. The signature header format is `sha256=<hex digest>`.

```python
import hashlib
import hmac
import json
import time

body = json.dumps({"data": {"id": "ord-1042", "customer": "acme"}}, separators=(",", ":")).encode()
timestamp = str(int(time.time()))
signature = hmac.new(WEBHOOK_SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
headers = {
    "Content-Type": "application/json",
    "X-Orchestrator-Timestamp": timestamp,
    "X-Orchestrator-Signature": f"sha256={signature}",
    "Idempotency-Key": "orders:ord-1042:v1",
}
```

Mapping paths are read-only paths such as `payload.data.id`; expressions and
code are not evaluated. An empty mapping passes the JSON object through. The
endpoint rejects stale timestamps and invalid signatures, enforces the
configured per-trigger request limit, and keeps a delivery ledger. Repeating
the same idempotency key and body returns the original run; reusing the key
with different content is a conflict.

Real-world use: a commerce provider can map an order ID and customer from its
event body into the workflow input, then retry delivery safely when a network
response is lost.

Workflow-success triggers start another published workflow after a source
workflow succeeds. They use the same safe JSON-path mapping against
`payload.run_id`, `payload.status`, and `payload.output`. Trigger creation
rejects cycles, and a mapping that does not exist in a particular run output is
recorded as a skipped trigger event without undoing the successful source run.

```json
{
  "name": "Archive completed invoices",
  "kind": "workflow_success",
  "source_workflow_id": "published-invoice-workflow-id",
  "input_mapping": {"source_run_id": "payload.run_id", "invoice": "payload.output.render.output"}
}
```

### Schedule calendars, intervals and backfills

Schedules can set `data_interval_seconds`, deterministic `jitter_seconds`,
`skip_weekends`, `skip_dates` in the schedule timezone, and absolute ISO 8601
`pause_windows` in that timezone. The next-run preview includes these calendar
rules. Deterministic jitter is capped before the next cron occurrence so a
delayed execution retains its intended logical slot. A scheduled run records its `logical_date`, `interval_start`, and
`interval_end` separately from the user's original run input; workflow
templates can read `input.logical_date` and `input.data_interval` without
changing the saved input payload. When no fixed interval is supplied, the
interval starts at the previous cron occurrence.

Real-world use: a daily accounting export can skip weekends and company
holidays, jitter across many customer schedules, and still receive an exact
logical interval to query from a warehouse.

`POST /api/v1/schedules/{schedule_id}/backfill` accepts a half-open UTC range
(`start` inclusive, `end` exclusive) and `concurrency_limit` from 1 to 64. A
durable backfill job creates the matching cron occurrences up to that limit;
the scheduler continues filling available slots until the range is drained.
Runs retain their scheduled logical dates and link back to the backfill job.
Use `GET /api/v1/schedules/{schedule_id}/backfills` to inspect job status.

```json
{
  "start": "2026-01-01T00:00:00Z",
  "end": "2026-02-01T00:00:00Z",
  "concurrency_limit": 4
}
```

The Schedules page exposes these options, displays backfill status, and treats
the selected end date as inclusive by converting it to the next day's UTC
midnight for the API's half-open interval.

## Phase 3: scale, fairness and reliability

### Claiming, queues and tenant fairness

On PostgreSQL, task claims select eligible rows with `FOR UPDATE SKIP LOCKED`
before assigning a lease. SQLite retains the conditional-update claim path.
Composite status/queue/priority/availability indexes support both paths.
Workers can register queue names (`WORKER_QUEUES=default,gpu`); each run can set
`priority` and `queue`, while a step can override either. Claims first favor
owners with fewer active tasks, then order eligible work by priority and age.
Long polling is enabled by default for workers for up to
`WORKER_LONG_POLL_SECONDS`; with Redis configured, the existing outbox stream
wakes waiting claims. Without Redis, the API checks the database at a bounded
interval while the claim is waiting.

Real-world use: route GPU inference to a dedicated pool while keeping urgent
customer-facing runs ahead of routine batch work, without one busy tenant
occupying every worker slot.

```json
{"steps": [{"id": "embed", "type": "ml.embed", "queue": "gpu", "priority": 100, "input": {}}]}
```

The run-start request can additionally pass `{"priority": 80, "queue": "gpu"}`;
steps inherit those values unless they specify their own.

### Concurrency and rate controls

Set `MAX_GLOBAL_RUNNING_TASKS`, `MAX_RUNNING_TASKS_PER_OWNER`, and the JSON
`QUEUE_CONCURRENCY_LIMITS` map to cap active work. A step can set
`concurrency_key` and `concurrency_limit` to coordinate shared resources across
workers. Those gates are stored and locked transactionally in the database.
Use `rate_limit_per_minute` and optional `rate_limit_key` on a step, or the
`TASK_RATE_LIMITS` JSON map by task type, for owner-scoped token buckets. These
controls regulate dispatch; a task already running keeps its lease until it
finishes or expires.

Real-world use: cap concurrent queries to a production database and limit each
tenant's calls to a provider such as an embeddings or payments API.

```json
{
  "steps": [
    {"id": "query", "type": "sql.query", "queue": "database", "concurrency_key": "db-prod", "concurrency_limit": 2, "input": {}},
    {"id": "embed", "type": "ml.embed", "rate_limit_per_minute": 60, "rate_limit_key": "embeddings-provider", "input": {}}
  ]
}
```

### Failure handling and dead letters

Workers classify common programming errors as permanent, network timeouts as
retryable, and HTTP 408/409/425/429 or 5xx responses as retryable. A handler can
set its own `retryable` attribute. Step retry settings support bounded
exponential backoff, a per-step maximum delay and jitter. The database-backed
circuit breaker opens after repeated retryable failures of one task type for an
owner. Repeated lease expiries move a poison task to `failed` early. Inspect
owner-scoped failures at `GET /api/v1/dlq` and redrive with
`POST /api/v1/dlq/{step_run_id}/redrive`; redrive resets that step and its
downstream dependents using the existing run retry path, emits one
`run.retry_requested` event, and is idempotent: repeating the call while the
step is already scheduled returns `already_redriven: true` without scheduling a
second execution.

Real-world use: stop sending traffic to a failing connector, keep permanently
invalid requests out of retry loops, and let an operator reprocess a corrected
task from the dead-letter list.

Workers stop claiming when they receive a shutdown signal, wait for in-flight
handlers up to their grace period, then call the shutdown endpoint to release
remaining leases. Lease tokens and task idempotency keys keep repeated reports
from completing the same attempt twice; external side effects still need to use
the provided idempotency key because delivery is at-least-once.

### Demo workflow

`python -m app.cli seed-demo` also installs **Queue priorities and dead
letters**: urgent work on a `priority` queue ahead of `batch` work, a step
holding the `demo-shared-resource` concurrency key, `demo.fail_once` recovering
on its second attempt with backoff and jitter, and a `demo.add` step with an
invalid `values` input that fails permanently and lands in the dead-letter list
for a redrive once its input is corrected.

### Claim throughput benchmark

Create a published workflow with many independent, ready steps and mint a
worker credential (`python -m app.cli create-worker`). Set `WORKER_ID` and
`WORKER_TOKEN`, then run `python scripts/load_test_claims.py --duration 30
--workers 8`. The script registers the worker (so a previously drained worker
comes back online), claims with that many concurrent requests, and completes
what it claims so the backlog drains and the rate reflects dispatch plus result
reporting; pass `--no-complete` to measure pure claim dispatch instead. It
reports the dispatch backend, the queue, the number of concurrent claimers,
claim requests/sec, tasks claimed/sec, tasks completed, client-observed claim
latency p50/p95/mean, and the duplicate-claim count (a task handed out for a set
the worker already advertised, or twice in one response, which must be 0).
Re-sent live assignments caused by concurrent snapshotting are reported
separately. It exits non-zero when a request failed or a duplicate claim was
observed, and shuts the worker down at the end to release outstanding leases.
Run it against the target PostgreSQL/Redis deployment for a meaningful
production comparison; SQLite numbers are local development measurements only
and include SQLite's single-writer contention.

## Phase 3B: the interactive dashboard

Every control on every page works. The dashboard is a live control center:
auto-refresh (Off/5s/10s/30s/60s, remembered per browser, paused while the tab
is hidden with an "Updated Xs ago" indicator), a 1h/24h/7d/30d window selector
backed by `DEFAULT_DASHBOARD_WINDOW_HOURS`, clickable stat cards that jump to
pre-filtered views, inline SVG charts for runs over time and duration trend
(each with a screen-reader data table), row actions on recent runs (cancel,
pause/resume, retry failed steps, re-run from step), an "needs attention" queue
that approves or rejects human steps in place, and a quick-actions bar that
starts workflows, seeds the demo (`POST /api/v1/demo/install`) or mints tokens.

The **Runs** page (`/runs`) searches, filters by status, workflow, trigger and
date range, sorts (newest/oldest/longest/status), saves filter presets locally
and runs confirmed bulk cancel/retry/delete with a per-item outcome summary.
**Workers & queues** (`/ops/workers`) shows the fleet with stale highlighting,
activates/deactivates or gracefully drains workers, inspects in-flight tasks and
runs the maintenance actions (recover leases, prune outbox, scheduler tick).
**Dead letters** (`/ops/dlq`) lists failed steps with their errors and redrives
them singly or in bulk; redrive is idempotent and repeats report
`already_redriven` as a notice, not an error.

Destructive controls anywhere in the UI (delete workflow, archive, delete
schedules/triggers/runs/secrets, revoke tokens, cancel runs) always ask for
confirmation first, then report the result as a toast. A light/dark theme toggle
sits in the header and persists. Real-world use case: an operator opens the
dashboard in the morning, sees "3 dead letters · 1 approval needed", approves
the invoice step, opens dead letters, bulk-redrives the failures whose input was
fixed overnight, and watches the runs page clear — without touching the API.

### Dashboard API surface

- `GET /api/v1/runs/dashboard?window_hours=N` now also returns
  `needs_attention`, `runs_timeline` and `duration_trend` (all additive).
- `GET /api/v1/runs` accepts `sort` (`newest|oldest|longest|status`) and
  `created_before` in addition to the existing filters.
