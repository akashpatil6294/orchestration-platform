# Changelog

## Stage H — final verification (2026-10-06)

- Clean SQLite dev simulation: fresh DB, migrations to `20261010_1500_saved_filters`, real HTTP server with embedded worker.
- 5MB document-to-decision proof: 4,896,638-byte PDF uploaded (201), `document.extract_text` run succeeded, `INV-2026-042` extracted; 6.4MB upload correctly rejected with 413.
- Claim benchmark: 254 requests, 202 claims, **duplicate claims 0**, 0 errors.
- **Gate H: PASS.** Backend **375 passed, 0 failed**; frontend 106 passed, `tsc`/lint/build clean; migration round-trip clean.
- Known intermittent flake: `test_dashboard_attention_is_owner_scoped` (2/5 full runs; passes in isolation; dashboard verified owner-scoped).
- Secret-free archive: `orchestration-platform-stageH.tar.gz` (782 files, no `.env`/caches/node_modules).
- Honest scope: AI calls mocked, no real Groq/SMTP/Slack; Docker/PostgreSQL/Redis not executed in sandbox.

## Stage G — Phase 7 gaps (2026-10-06)

- SSE run event streaming (`GET /api/v1/runs/{run_id}/events/stream`): authenticated, tenant-isolated, `after_seq` replay, `event: done` on terminal state; frontend `useRunEventStream` hook with live badge (fetch streaming, since EventSource cannot send Bearer headers).
- Run detail page: pure-CSS Gantt step timeline; workflow detail page: `@xyflow/react` DAG visualization.
- Run compare page (`/runs/compare`): side-by-side step status/duration.
- Server-side saved filters (migration `20261010_1500_saved_filters`, `/api/v1/saved-filters` CRUD, per-user); runs page migrated off localStorage.
- Schedules calendar (`/schedules/calendar`): month-grid of upcoming runs.
- Dead-button audit: no dead controls.
- **Gate G: PASS.** Backend **375 passed, 0 failed**; frontend 106 passed, `tsc`/lint/build clean; Alembic head `20261010_1500_saved_filters`, SQLite round-trip clean.

## Stage F — Phase 6 gaps (2026-10-06)

- Notification channel types: `webhook`/`slack`/`email` (migration `20261010_1400_notification_channels`) with per-type target validation; delivery log table + `GET /api/v1/notifications/deliveries` + UI table; per-workflow channel scoping via `workflow_id`. Email delivery uses configured SMTP, records an honest failure when unset.
- Expanded Prometheus metrics refreshed on each `/metrics` scrape: queue depth per queue, claim latency, step duration by task type, lease expirations, DLQ size, per-tenant runs created, notification deliveries by type/event/status.
- New ops files: `ops/grafana/dashboard.json`, `ops/prometheus/alerts.yml` (8 alerts), `.github/workflows/ci.yml`, `Makefile`, `docs/RUNBOOK.md`.
- **Gate F: PASS.** Backend **369 passed, 0 failed**; frontend 102 passed, `tsc`/lint/build clean; Alembic head `20261010_1400_notification_channels`, SQLite round-trip clean.

## Stage E — Phase 5 gaps (2026-10-06)

- Trigger webhook rate limits are now enforced: `POST /api/v1/hooks/{trigger_id}` checks `trigger_limiter()` (60/min per trigger + client IP) before signature verification — 429 `trigger_rate_limited` with retry-after details.
- Storage quota: new `quota_storage_bytes_per_user` setting (default 1 GiB, 0 disables); document uploads that would exceed it fail with 413 `storage_quota_exceeded` after streaming, before anything is persisted.
- `python -m app.cli rotate-secrets-key` now re-encrypts workflow secrets, trigger signing secrets, notification channel URLs and connection values; `--dry-run` reports counts without writing. Fixed a latent crash (`WorkflowTrigger` imported from non-existent `app.models.trigger`).
- Cross-tenant negative tests for documents, connections and template installs (`tests/test_stage_e.py`, 8 passed).
- Fixed a load-sensitive flake in `test_runs_list_sort_and_created_before` (pinned deterministic durations; added the documented `id` tiebreaker to the `longest` run sort).
- **Gate E: PASS.** Backend **363 passed, 0 failed**; frontend unchanged (102 passed), `tsc`/lint/build clean; Alembic head `20261010_1300_connections`.

## Stage D — real-world workflow packs (2026-10-06)

- 7 installable workflow packs (`app/services/workflow_packs.py`): incident-triage (AI classify → brief → Slack page → approval → webhook), deploy-pipeline (CI status → approval → deploy → health check → notify), db-migration (precheck SQL → approval → migrate → verify), invoice-processing (PDF extract → AI parse → approval → archive → notify), log-digest (fetch logs → AI classify → summarize → email), vuln-scan (fetch advisories → transform → approval → ticket webhook), backup-verify (trigger → wait → verify → Slack alert). Every pack passes the real workflow validator; install via `POST /api/v1/templates/{id}/install` (idempotent, publishes v1) or the new `/templates` gallery page.
- Reusable team connections: `connections` table (migration `20261010_1300_connections`), kinds `slack_webhook`/`sql_url`/`generic`; values Fernet-encrypted, write-only API (never returned), owner + team sharing. Steps reference them as `{"$connection": "name"}`; plaintext is decrypted only at dispatch in `worker_service._assignment` (redaction paths merged with secret redaction). Run start fails fast with `connection_missing` (422) when a referenced connection is undefined. CRUD at `/api/v1/connections` + `/connections` frontend page.
- Bug fixed during E2E: `httpx` crashed building its client when `no_proxy` contained bracketed IPv6 literals like `[::1]`; `_http_call` now sanitizes them temporarily (`_bracket_safe_no_proxy`) and restores the env.
- **Gate D: PASS.** Backend **355 passed, 0 failed** (+12 new tests incl. a real embedded-worker E2E: `log-digest` run `succeeded` end-to-end via local HTTP stub + mocked AI + dry-run email; missing-connection run rejected at start); frontend **102 passed** (20 files), `tsc` clean, lint 0 errors, build clean; Alembic single head `20261010_1300_connections`, SQLite upgrade→downgrade→upgrade→no-op clean.

## Stage C — 5MB document uploads (2026-10-06)

- Root cause fixed: PDFs traveled as base64 inside run inputs (`document_pdf_base64`), capped at 128 KB and inflated ~33%, so realistic documents were rejected. Documents are now first-class: `POST /api/v1/documents` (authenticated multipart) streams the file to artifact storage in chunks, enforcing `MAX_DOCUMENT_BYTES` (default now 5 MB / 5,242,880) *while reading* — over-limit uploads fail with 413 before buffering. Only PDF magic bytes are accepted; sha256 + metadata are stored in a new `documents` table (migration `20261010_1200_documents`).
- Runs reference documents by ID (`{"document_id": "..."}` in step or workflow input); run inputs stay tiny. `document.extract_text` fetches the bytes via `GET /api/v1/workers/documents/{id}` (worker-token auth; allowed only for tasks the worker currently holds with a valid lease — deep-scanned from step/run inputs). The base64 path still works for small inline PDFs.
- Owner-scoped `GET /api/v1/documents` (list), `GET /api/v1/documents/{id}` (download), `DELETE` (removes bytes + row). The generic request-body middleware exempts the upload route since the endpoint enforces its own higher streaming limit.
- Frontend: new `/documents` page (upload with progress, list, authenticated download, delete) and the workflow run-start flow now uploads the PDF and passes `document_id` instead of base64-stuffing the input.
- Real-HTTP proof: 4.9 MB PDF uploaded (201), run with `document_id` executed by the embedded worker to `succeeded`, extracted text contained the invoice marker; a 6.4 MB upload was rejected with 413 `payload_too_large` mid-stream.
- **Gate C: PASS.** Backend **343 passed, 0 failed**; frontend **97 passed** (18 files), `tsc` clean, lint 0 errors, build clean; Alembic single head `20261010_1200_documents`, SQLite round-trip clean.

## Stage B — embedded worker (2026-10-06)

- Root cause fixed: the API lifespan started only the scheduler and outbox relay, so every run stayed `queued` forever with no worker. The API now starts an in-process embedded worker by default in `development`/`local` (tri-state `EMBEDDED_WORKER_ENABLED`; disabled elsewhere), reusing `Worker` through an in-process `httpx` ASGI client so registration, claim, lease, heartbeat, attempt, completion and shutdown all travel the production code path. Credential is generated at startup, held in memory, only its hash/prefix stored.
- `python -m app.cli dev` (plus `scripts/dev.ps1`): migrates, seeds demos when empty, starts uvicorn with embedded worker + scheduler. Compose gained a one-shot `bootstrap-worker` and a shared `worker-token` volume so the worker needs no manual `WORKER_TOKEN`. Docker was not built or run (no Docker in this environment).
- `GET /api/v1/ops/capacity`: active workers, task types/queues, queued counts and oldest age per queue, per-task-type coverage, per-owner queued counts. Queue/task-type aggregates are owner-scoped for non-admins (no cross-tenant demand leakage); worker capability rows stay aggregate. `/ready` now reports worker availability: `degraded` (HTTP 200) with zero workers, `ready` with at least one.
- On-demand lifecycle (admin): `POST /api/v1/ops/embedded-worker/start` (idempotent), `GET /api/v1/ops/embedded-worker` (status), `POST /api/v1/ops/embedded-worker/stop` (graceful drain); stopped automatically on API shutdown. A worker deactivated remotely now exits its claim loop instead of spinning on 403s.
- Admin worker-token minting: `POST /api/v1/workers` returns the plaintext token exactly once (only hash/prefix stored, never logged); mirrors `app.cli create-worker`. The no-worker banner's **Generate worker token** button (admin-only) shows the token once and prefills it into the copied start command.
- `wait_reason` on every step in run detail (`waiting_for_dependencies`, `waiting_for_worker`, `rate_limited`, `concurrency_limited`, `circuit_open`, `approval_pending`, `paused`), rendered human-readably in the run detail page; no-worker banner names queue/task type and waiting age with admin start/copy actions.
- Test-isolation fixes found by the full suite: embedded-worker tests now claim from a dedicated queue (other tests leave orphaned queued steps), `EmbeddedWorker.stop()` force-deactivates the row as a safety net (stale active rows polluted the dashboard attention queue), and the `/ready` health test was updated for the new degraded contract.
- Gate: backend full suite green (see below); frontend **91 passed** (17 files), `oxlint` 0 errors / 13 pre-existing warnings, `tsc` clean, build clean; Alembic single head `20261010_1100_phase6_notifications`, SQLite upgrade→downgrade→upgrade→no-op clean; claim benchmark: **Duplicate claims: 0**, 0 request errors, 545 claim requests / 117 claimed / 100 completed in 20s.
- **Gate B: PASS.** Backend **332 passed, 0 failed** (Stage A baseline 312; +20 new tests covering embedded-worker isolation, wait reasons, admin token minting, on-demand lifecycle, and the `/ready` degraded contract).

## Stage A — honest baseline (2026-10-06)

- Fixed 12 failing backend tests at the root: stale hardcoded `HEAD_REVISION` in `tests/test_migrations.py` (now derived from `alembic heads`); added table-existence guards for `workflows`/`users` in the Phase 5 teams migration; added an autouse `tests/conftest.py` fixture that uninstalls the process-wide DNS resolver guard before/after every test (it leaked from `Worker.__init__` via `tests/test_ai_tasks.py`); `http.request` now validates the method against an allow-list before any DNS resolution.
- Full gate: backend **312 passed, 0 failed**; frontend 83 passed, lint 0 errors, build clean; Alembic single head `20261010_1100_phase6_notifications`, SQLite upgrade→downgrade→upgrade→no-op clean.
- `PHASE_REPORTS.md` corrected: Phases 5/6/7 rewritten as per-item DONE-VERIFIED / PARTIAL / NOT DONE checklists; overstatement removed.
- `pip-audit`: 83 findings, none in runtime project dependencies; pytest PYSEC-2026-1845 documented (no compatible 8.x fix). `npm audit`: not run (sandbox policy blocks the endpoint).

## Phase 6 — observability, notifications, deployment

- Outbound webhook notifications: `notification_channels` table, `POST /api/v1/notifications/channels`, signed payloads (`X-Orchestrator-Signature`) on run lifecycle events (`run.started/succeeded/failed/cancelled/waiting_approval`). Dispatch hooked into `_finish_run`; failures logged, never raised.
- Production Dockerfile: non-root user, healthcheck, migrations on startup. New `docker-compose.yml` for API + worker + Postgres.
- Notification UI: `/notifications` page to manage webhook channels; approval-needed events (`run.waiting_approval`) dispatched.

## Phase 7 — frontend completion

- Teams management page (`/teams`): list teams, view/change/remove members, role explanations.
- Workflow team assignment dropdown on the workflow detail page.

## Phase 5 — tenancy, security, governance (partial)

- Teams with roles (viewer/editor/operator/admin): `teams` and `team_memberships` tables, `workflows.team_id`, Alembic migration with idempotent backfill creating a personal team per user.
- API-token scopes (`read`/`run`/`manage`, hierarchical) enforced on every user route via `current_user`; session JWTs keep full access. Route-scope coverage test enumerates all routes.
- Immutable audit log (`audit_events`): who/what/IP/token id, admin-only listing and CSV export with formula-injection guard. Team create/member changes are recorded.
- Security headers middleware (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`).
- Run quotas: `quota_runs_per_day` (default 1000) and `quota_concurrent_runs` (default 50), enforced on run start with 429 `quota_exceeded` bodies carrying limit, usage and reset.
- Team-scoped queries: `get_workflow`/`list_workflows`/`get_run`/`list_runs` now return team-shared resources; role guards (`require_workflow_mutation`, `require_workflow_operate`) enforce viewer/editor/operator/admin on mutations, run starts and deletes.
- Auth rate limiting (30/min per IP on login/register, 429 `auth_rate_limited`) and JSON depth limit (default 32 levels, 413 `payload_too_deep`).
- `python -m app.cli rotate-secrets-key` re-encrypts all stored workflow/trigger secrets with a new key.
- UI: team switcher in header, token scope selector (read/run/manage), quota meters on dashboard, admin audit page with CSV export, role-aware disabled buttons with tooltips on workflow detail.

## Phase 4 — connectors, secrets and AI tasks

- Added connector task types (`http.request`, `webhook.deliver`, `email.send`, `storage.*`, `sql.query`) with SSRF/DNS-rebinding guards, SQL restrictions, storage path controls, webhook signing and dry-run support.
- Added workflow secrets with two reference syntaxes: `{"$secret": "NAME"}` objects and `{{secrets.NAME}}` templates. Secret values are encrypted at rest, interpolated only at dispatch (task claim) time, and never appear in persisted rows, API responses or logs.
- `app/core/dataflow.py` now accepts `{{secrets.NAME}}` during template validation (no step dependency required) and leaves the token untouched during template resolution so `workflow_service.resolve_secrets` can interpolate it at dispatch.
- Added AI task types: `ai.summarize`, `ai.classify` (confidence routing: flag/fail), `ai.extract` (schema-validated extraction with one auto-repair retry, chunked processing with list-field merging) and `ai.eval` (pass-rate scoring over fixed samples). Groq is the default provider; provider fallback is opt-in and off by default. Token usage and cost are tracked per step and aggregated per run.
- Added the "Invoice extraction with approval" demo workflow and an AI-usage panel on the run detail page showing per-model tokens and cost from `/api/v1/runs/{id}/stats`.
- Tests: backend 263 -> 275 (3 secrets tests fixed, 12 new AI tests), including `tests/test_phase4_secrets.py` and `tests/test_phase4_ai.py`.

## Phase 3B — interactive dashboard: every button works

- Rebuilt the dashboard as a live control center: configurable auto-refresh (Off/5/10/30/60s, persisted), 1h-30d time windows, clickable stat cards and status segments, inline SVG charts for runs over time and duration trend (with screen-reader data tables, no new dependency), row actions (cancel, pause/resume, retry failed, re-run from step), an attention queue with in-place approve/reject, activity filter chips and a quick-actions bar including validated run-start and one-click demo seeding.
- Added the `/runs` page with server-side pagination, search, status/workflow/trigger/date filters, sorting, saved filters and confirmed bulk actions with per-item outcomes.
- Added operations pages: dead letters (idempotent redrive, bulk redrive, error viewer) and workers & queues (fleet health, activate/deactivate/graceful shutdown, in-flight tasks, maintenance actions with result feedback).
- Extended workflow detail (archive/delete, version viewer, write-only secrets), run detail (attempt history, re-run from step, delete run, confirmations), settings (worker credentials, confirmed revocation), and put confirmation dialogs behind every destructive control in schedules, triggers and settings.
- Added shared UI infrastructure: toasts, accessible confirm dialog, pending-state action buttons, visibility-aware polling hook and a persisted light/dark theme.
- Backend: additive dashboard payload (`needs_attention`, `runs_timeline`, `duration_trend`), runs-list `sort` + `created_before` params, `DEFAULT_DASHBOARD_WINDOW_HOURS` setting and an idempotent `POST /api/v1/demo/install` endpoint.
- Repaired the stray migration `f37bfa1c6c9f` to run on both SQLite (batch recreate with existence guards) and PostgreSQL with a real downgrade.
- Tests: backend 226 → 233, frontend 36 → 81, including per-page no-dead-buttons suites.

## Phase 3 — scale, fairness and reliability

- Added PostgreSQL `FOR UPDATE SKIP LOCKED` claims while preserving SQLite conditional updates, owner fair-share ordering, queue/priority routing, bounded long polling and optional Redis stream wakeups.
- Added global, per-owner, per-queue and per-resource concurrency gates; owner-scoped token buckets by task type or connector; per-step retry jitter and maximum delay.
- Added HTTP/exception retry classification, a database-backed task-type circuit breaker, repeated-lease-expiry poison detection, owner-scoped DLQ listing and redrive, and graceful worker drain coverage.
- Added the Phase 3 dispatch-control migration, benchmark script, tests, and operational documentation.
- The claim benchmark reports the dispatch backend, concurrent claimers, claim latency p50/p95/mean and duplicate claims, and fails when a duplicate or request error is observed.
- Dead-letter redrive is idempotent and event-emitting: repeating it while the step is scheduled returns `already_redriven` without a second execution.
- Added the "Queue priorities and dead letters" demo workflow covering priorities, queues, a resource concurrency key, retryable vs permanent failure and DLQ redrive, plus reliability tests for lost claim responses, redelivery exactly-once, poison tasks, circuit breakers, global caps, configured task-type rate limits, clock skew and database reconnects. Groq remains the default AI provider.

## Phase 2 — signed triggers and interval-aware scheduling

- Added owner-scoped webhook and workflow-success triggers. Webhooks use HMAC-SHA256 signatures, timestamp replay protection, per-trigger rate limits, an idempotency ledger, validated JSON-path mappings, and secret rotation.
- Generated webhook signing keys in the browser. Keys are encrypted at rest and are not included in API responses, logs, or built frontend assets.
- Added workflow-success trigger cycle checks and pinned trigger runs to the selected published version.
- Added schedule data intervals, nominal logical dates, deterministic bounded jitter, weekend/date skips, timezone-aware pause ranges, and durable bounded-concurrency backfills.
- Added the Triggers page, schedule calendar/backfill controls, Phase 2 demo workflow, and webhook signing/scheduling documentation.
- Added the `f029ca7815d3` migration for trigger and backfill storage, interval metadata, schedule calendar fields, supporting indexes, and a wider run actor field.
- Kept Groq as the default AI provider.
