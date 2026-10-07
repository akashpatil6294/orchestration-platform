# HANDOFF — Orchestration Platform: resume point (mid Phase 4)

Paste everything below into a fresh agent session opened at
`C:\Users\akash\OneDrive\Desktop\orchestration platform`.

---

## 0. Mission

Continue the existing workflow orchestration platform (FastAPI, SQLAlchemy, Alembic,
React 19 / Vite / TypeScript, Groq as the AI provider). Phases 1–3 were already
complete; **Phase 3B is now also COMPLETE and gate-green**. **Phase 4 is roughly
70% done** — the exact remaining work is in section 4 below. Phases 5, 6, 7 follow.

## 1. Verified current state (all numbers from commands actually run)

- Backend: `python -m pytest -q` → **233 passed** (this was BEFORE the phase-4
  test files were added; current working tree adds 30 passing connector tests and
  3 FAILING secret tests, so expect 260 passed / 3 failed until section 4.1 is done).
- Frontend: `npm test` → **81 passed** (16 files); `npm run lint` → **0 errors,
  11 warnings** (9 pre-existing + 2 new `only-export-components` warnings from
  `ToastProvider`/`ConfirmDialogProvider` exporting a hook next to the provider —
  matches the existing AuthProvider/StatusView convention; do not grow it further);
  `npm run build` → ok.
- Alembic: one head `f37bfa1c6c9f` (down_revision `81c7e9a13b42`). Fresh-SQLite
  `upgrade head → downgrade -1 → upgrade → upgrade (no-op)` all green. `alembic
  current` reports **PostgresqlImpl** — a real PostgreSQL DB is reachable via the
  user's `DATABASE_URL` and sits at head; NEVER run destructive DDL on it. No new
  migrations were added in 3B/4 so far.
- `PHASE_REPORTS.md` contains: baseline re-verification note, the 3B.0 button
  inventory, and the complete 3B result section. `CHANGELOG.md` has a 3B entry.
  `README.md` has a "Phase 3B" section.
- Windows machine: use `.venv\Scripts\python -m pytest` (or
  `".venv/Scripts/python" -m pytest` in Git Bash). Never read `.env`.

## 2. What Phase 3B delivered (for context; do not redo)

- Shared UI: `ToastProvider`, `ConfirmDialogProvider` (promise-based confirm,
  focus trap, Escape), `ActionButton` (pending state + error toasts),
  `lib/useLiveResource.ts` (visibility-aware polling, no overlap, Updated-Xs-ago),
  `lib/theme.ts` (light/dark persisted), `components/Charts.tsx` (inline SVG,
  no dependency), CSS additions incl. light palette in `frontend/src/index.css`.
- Pages: Dashboard rewritten (auto-refresh selector, 1h/24h/7d/30d window,
  clickable stat cards + status segments, run row actions cancel/pause/resume/
  retry/rerun-from-step/open workflow, needs-attention panel with inline
  approve/reject, activity filter chips, quick actions incl. Run demo and a
  validated run-workflow panel); new `/runs` page (search, filters, date range,
  sort, saved filters, confirmed bulk actions with per-item summary); new
  `/ops/dlq` (idempotent redrive, bulk redrive, error viewer); new
  `/ops/workers` (fleet + maintenance actions); WorkflowDetail (archive/delete,
  version viewer, write-only secrets); RunDetail (attempts viewer, rerun from
  step, delete run, confirms); Settings (worker credentials, revoke confirm);
  Schedules/Triggers delete confirms. Routes registered in `App.tsx`.
- Backend additions: `build_dashboard` returns additive `needs_attention`
  (with `step_key` for approvals), `runs_timeline`, `duration_trend`;
  `GET /api/v1/runs` gained bounded `sort` (`newest|oldest|longest|status`) and
  `created_before`; `DEFAULT_DASHBOARD_WINDOW_HOURS` setting (1–720, in
  `.env.example`); idempotent `POST /api/v1/demo/install`;
  `tests/test_phase3b_dashboard.py` (7 tests).

## 3. What Phase 4 work has ALREADY landed (all in the working tree, uncommitted)

1. `app/config.py`: added `connector_sql_allow_writes: bool = False` and
   `connectors_dry_run: bool = False` (both also in `.env.example` with docs).
2. `app/services/workflow_service.py`: `referenced_secret_names` and
   `resolve_secrets` now support BOTH `{"$secret": "NAME"}` objects AND
   `{{secrets.NAME}}` templates inside string values (regex
   `\{\{\s*secrets\.([A-Za-z0-9_.-]+?)\s*\}\}`; paths recorded in redacted_keys;
   unknown names raise `Invalid` code `secret_missing`).
3. `app/worker/connector_tasks.py` (NEW): plain functions
   `http_request`, `webhook_call` (HMAC-SHA256 `sha256=<hex>` over
   `"{timestamp}." + body` with X-Orchestrator-Timestamp/Signature headers),
   `slack_post` (host allow-list), `email_send` (smtplib, header-injection
   guards, dry-run), `sql_query` (parameterized via sqlalchemy `text()`,
   read-only unless `connector_sql_allow_writes`, single-statement check, row
   limit, PG `statement_timeout`), `storage_put`/`storage_get` (DESIGN DECISION:
   put returns the validated blob as the step OUTPUT — `runs/{run_id}/{key}`
   namespacing — so large payloads become authenticated run artifacts via the
   existing `store_if_large` path; get reads a prior step's dependency output;
   key regex forbids traversal), `transform_json` (path/map/filter/merge/literal
   rules, no eval, depth/item caps). Do NOT register them here — see 4.
4. `app/worker/handlers.py`: registers everything in ONE place —
   `ai.extract` → `extract_structured`, `ai.eval` → `evaluate_extraction`,
   plus `http.request`, `webhook.call`, `slack.post`, `email.send`,
   `sql.query`, `storage.put`, `storage.get`, `transform.json`.
5. `app/worker/connectors.py`: `require_secret_field` now suffix-matches
   redacted paths (dispatch records them relative to the step input root, e.g.
   `auth.value`); added `install_resolver_guard(exempt_hosts)` /
   `uninstall_resolver_guard()` wrapping `socket.getaddrinfo` so connect-time
   DNS (incl. httpx's own lookups) refuses private/loopback/link-local/
   metadata/blocked-CIDR answers — defeats DNS rebinding; `_http_call` wraps
   `socket.gaierror` as `ConnectorTransientError`.
6. `app/worker/runtime.py`: `Worker.__init__` installs the resolver guard with
   the orchestrator API host exempt.
7. `app/worker/ai_tasks.py`: `_groq_generate`/`_gemini_generate`/`_generate`
   return `(text, usage)`; provider fallback wired (`ai_fallback_enabled` +
   `ai_fallback_provider`, OFF by default, only on `AIProviderRequestError`);
   `_usage_entry` normalizes usage and computes `cost_usd` from
   `settings.ai_model_prices`; `_chunk_text` (ai_chunk_chars / overlap
   capped at size//2, max 64 chunks); `extract_structured` (mini JSON-schema
   validator: type/properties/required/items/enum; auto-repair retries from
   `ai_extract_repair_attempts` with problems fed back; per-chunk merge:
   lists concat, objects last-non-empty-wins; output key `extracted`);
   `evaluate_extraction` (≤25 samples, per-sample pass/fail, pass_rate);
   `classify_text` now asks for confidence 0–1, supports `min_confidence`
   (falls back to `ai_classify_min_confidence`) and `on_low_confidence`
   `flag`|`fail`; summarize/classify outputs include `ai_usage`.
   NOTE: `_generate` returns a TUPLE now — tests that monkeypatch it must
   return `(text, usage)`.
8. `app/services/run_service.py`: new `ai_usage_for_run(db, run_id)` aggregates
   `ai_usage` from step outputs (exported in `__all__`); `app/api/runs.py`
   `GET /api/v1/runs/{id}/stats` now includes `**run_service.ai_usage_for_run(...)`.
9. `tests/test_connectors.py` (NEW): **30 passed** — SSRF matrix (private v4/v6,
   loopback, link-local, metadata hosts, IPv4-mapped v6, embedded credentials,
   domain allow-list), per-hop re-resolution rebinding test, resolver-guard
   connect-time rebinding test, redirect-to-private block, oversized response,
   http auth secret enforcement + header/query injection, sql parameterized
   SELECT + read-only/single-statement/literal-secret refusals, storage
   roundtrip + traversal + cross-run read refusal, email/slack/webhook dry-runs,
   webhook HMAC signature verification, transform map/filter/merge/literal +
   eval refusal.

## 4. REMAINING WORK — Phase 4 (do this next, in order)

### 4.1 Fix the 3 failing tests in `tests/test_phase4_secrets.py`

Current failures, run `python -m pytest tests/test_phase4_secrets.py -q`:

1. `test_template_references_are_discovered_and_resolved` and
   `test_unknown_template_secret_is_rejected_before_dispatch` fail with
   `sqlite3.OperationalError: no such table: users` — they call `SessionLocal()`
   directly without the schema. FIX: add the session-scoped `app_module` fixture
   as a parameter to both tests (that fixture runs `Base.metadata.create_all`).
2. `test_secret_value_never_appears_in_persisted_rows_responses_or_logs` fails
   at workflow creation: `workflow_factory` gets 422
   `step.input_reference_invalid` because `app/core/dataflow.py::
   validate_templates` does not recognize `{{secrets.LEAKED}}`. FIX (production
   code, two places in `app/core/dataflow.py`):
   - `validate_templates`: accept expressions matching
     `secrets\.[A-Za-z0-9_.-]+` (validate the NAME pattern only; no
     dependency requirement) and continue.
   - `resolve_templates` (and its helper `_resolve_reference` path): when an
     expression is `secrets.NAME`, LEAVE THE STRING UNCHANGED so that
     `workflow_service.resolve_secrets` interpolates it later in the claim path
     (`worker_service` line ~642: `resolve_secrets(db, run.workflow_id,
     template_resolved_input)` runs AFTER template resolution). Preserve the
     existing semantics: a string that is exactly one reference keeps
     standalone-value semantics only for input/steps references — for
     `secrets.*` just return the string as-is in all cases (pure pass-through),
     and make sure the "unsupported data reference" error is not raised for it.
   Then re-run the file. The e2e leak test: creates a workflow whose echo step
   input is `{"token": "{{secrets.LEAKED}}"}`, stores the secret via
   `PUT /api/v1/workflows/{id}/secrets/LEAKED`, runs it, completes the task
   echoing the secret into output + logs, and asserts the value appears in NO
   API response, event, or persisted row (StepRun.output_data/logs/
   input_data). If the e2e test still trips on validation of the reference
   inside `{"token": ...}`, also check `_input_contains_secret_references` /
   `referenced_secret_names` are used consistently. Delete the stray
   `client.put(... DUMMY ...)` call at the top of the test (it is dead code).
3. Re-run the FULL backend suite and fix any fallout (existing dataflow tests
   may need the new pass-through behavior reflected; do not weaken old
   guarantees — input/steps references must still validate exactly as before).

### 4.2 Write `tests/test_phase4_ai.py` (not yet written)

Cover, with mocked `httpx.post` (pattern: `tests/test_ai_tasks.py`, `Context`
helper with `attempt=2`, `task`, `log`, `cancelled`):
- `ai.extract` happy path: Groq returns `{"choices": [{"message":
  {"content": "<json>"}}], "usage": {"prompt_tokens": 100,
  "completion_tokens": 20}}`; assert `extracted` matches schema, `ai_usage`
  normalizes tokens, and cost is computed when `settings.ai_model_prices` has
  the model (e.g. `{"openai/gpt-oss-120b": {"prompt": 0.15, "completion": 0.6}}`
  → cost = 100*0.15/1e6 + 20*0.6/1e6).
- Auto-repair: first response invalid per schema, second (repair) response
  valid; assert 2 provider calls and `repairs_used == 1` (settings default
  `ai_extract_repair_attempts=1`); and permanent failure when the repair also
  fails (AIProviderRequestError with retryable=False).
- Chunking: set `settings.ai_chunk_chars` small (e.g. 500) + overlap, feed a
  longer text, assert provider called once per chunk and list fields merged.
- `ai.classify`: confidence below `min_confidence` with
  `on_low_confidence="fail"` raises; with `"flag"` returns
  `low_confidence=True`; confidence missing → `low_confidence=False`.
- Fallback: `ai_fallback_enabled=True` + `ai_fallback_provider="groq"` with
  primary gemini raising `AIProviderRequestError` → fallback called; disabled
  → raises; fallback off by default.
- `ai.eval`: 2 samples (1 match, 1 mismatch) → `pass_rate == 0.5`.
- API: complete a run whose step output includes `ai_usage` and assert
  `GET /api/v1/runs/{id}/stats` aggregates tokens per model (or cover
  `ai_usage_for_run` directly against a session).

### 4.3 Demo + UI + docs

- `app/services/demo_data.py`: add an "Invoice extraction with approval" entry
  to `DEMO_ADVANCED_DEFINITIONS`: `document.extract_text` → `ai.extract`
  (json_schema: invoice_number/vendor/total; prompt_template stored in input —
  it is frozen into the version) → `approval` step → `storage.put`
  (key `invoices/{{input.invoice_id}}.json`? keep it a literal key to avoid
  template validation questions) → `slack.post` with `webhook_url`
  `{"$secret": "SLACK_WEBHOOK"}`. Check `tests/test_phase1_engine.py` demo-count
  assertion and `install_demo_workflows` idempotency still hold; add/adjust a
  test that the new demo installs and publishes.
- Run detail UI: show the aggregated AI usage (fetch
  `/api/v1/runs/{id}/stats` and render tokens/cost when `ai_usage` is present)
  in `frontend/src/pages/RunDetailPage.tsx` + a small test addition.
- README: add a "Phase 4: connectors, secrets and AI" section — connector
  table (each type + key guards), both secret syntaxes, dry-run flag, sql
  allow-writes admin flag, ai.extract example JSON, ai.eval, cost tracking, and
  a real-world invoice-processing use case with example JSON. CHANGELOG entry.
  PHASE_REPORTS: full Phase 4 section with raw evidence (commands + counts).

### 4.4 Phase 4 gate

Standard gate (backend pytest, frontend npm test/lint/build, alembic heads +
fresh-SQLite round trip — no new migration was added in Phase 4) **plus the
Phase 3 claim benchmark re-run**: start the API on 127.0.0.1:8099 with a SQLite
DB (see the exact command recorded in PHASE_REPORTS.md Phase 3 section) and run

```
python scripts/load_test_claims.py --api http://127.0.0.1:8099 --duration 20 --workers 8 --wait-seconds 0
```

Confirm `Duplicate claims: 0` and record raw numbers in PHASE_REPORTS.md.

## 5. Phase 5 (next, after 4): tenancy, security, governance

Teams/projects with roles viewer/editor/operator/admin; migration backfilling a
personal team for every existing row (idempotent, tested on a seeded legacy
DB); API-token scopes read/run/manage enforced on EVERY route (enumerate all
registered routes in a test and assert each declares a required scope); quotas
per user/team (runs/day, concurrent runs, storage) with clear 429 bodies
(limit, usage, reset); immutable audit log (append-only, who/what/IP/token id,
listing + CSV export with formula-injection guard); hardening (auth/trigger
rate limits, payload-depth limits, security headers, secret + encryption-key
rotation tool, pip-audit/npm audit — report real output or "not run");
cross-tenant negative tests for every new endpoint; small UI: team switcher,
role-aware disabled buttons with tooltip, token scope selector, quota meters,
audit log page with CSV download.

## 6. Phase 6: observability, notifications, deployment

OpenTelemetry (trace per run, span per step attempt, context to workers,
exporter OFF by default); Prometheus metrics expansion (queue depth, claim
latency, step duration histograms, retry/failure rates, lease expirations,
outbox lag, per-tenant usage; bounded cardinality) + importable Grafana
dashboard JSON + alert rules (validate YAML/JSON); notification alert rules
(failure, SLA breach, stuck run, worker offline, DLQ growth) via Slack/email/
webhook through the outbox with delivery log + UI editor and test-send;
docker-compose.yml (api, scheduler, N workers, PostgreSQL, Redis, one-shot
migration job) + separate worker Dockerfile + healthchecks + GitHub Actions CI
(ruff, mypy, pytest on SQLite and PostgreSQL, frontend lint/test/build, secret
scan) + Makefile/justfile + PowerShell equivalents + docs/RUNBOOK.md. Validate
YAML syntactically; state clearly what was NOT executed (Docker, compose up,
PostgreSQL CI, Redis) unless actually run.

## 7. Phase 7: frontend completion

Visual DAG editor (React Flow / `@xyflow/react`): palette drag/drop, edge
creation, inline API validation errors on nodes/edges, undo/redo, JSON⇄graph
toggle, version diff, rollback/pin, YAML/JSON import-export, auto-layout,
dirty-state guard. Live run view: SSE preferred (owner/team scoped backend
stream with heartbeat + Last-Event-ID reconnect) or WebSocket; timeline/Gantt,
critical path, attempt history, side-by-side run compare, approve/reject,
server-side saved filters. Final ops pages (schedules calendar + backfill
form, quotas/usage, audit viewer, alert rules + delivery log). Accessibility
pass, virtualized long lists (report measured numbers or "not run"), Vitest
for every new component, re-run the no-dead-buttons suite across ALL pages,
final updated button inventory with zero DEAD/BROKEN rows.

## 8. Rules that always apply

Never read/print `.env` (only `.env.example`); never echo secrets; keep secrets
out of logs/API responses/events/bundle. Groq stays the AI provider; fallback
providers configurable but OFF. Never evaluate workflow-supplied code —
expressions only via `app/core/expr.py`, handlers only via the registry. Every
schema change is a re-runnable Alembic migration working on PostgreSQL and
SQLite with existence guards and a real downgrade; exactly one head; run
`alembic heads` before adding. Every new query owner-scoped (team-scoped after
Phase 5) with cross-tenant negative tests. Follow existing conventions
(services in `app/services`, routers in `app/api`, `useResource`/`api`/
`StatusView` in the frontend, settings with bounds in `app/config.py` +
`.env.example`). Backward compatibility: existing endpoints/shapes/tests keep
working. Every number you report must come from a command you ran; write
"not run" for PostgreSQL round-trips, Redis, Docker, real networks, real Groq
calls. Do not start a phase while the previous gate has any failure. If budget
runs low, stop at a clean green point and state exactly what remains.
