# Runbook — Orchestration Platform

## Symptoms and fixes

### Runs stuck in `queued`
1. Check `/api/v1/ops/capacity`: are there active workers? Any workers covering the run's queue and task types?
2. Check Prometheus `orchestrator_workers_active` and `orchestrator_queue_depth{queue="<q>"}`.
3. If no workers: in development the embedded worker starts automatically (`python -m app.cli dev`). Otherwise mint a token (`POST /api/v1/workers`, admin) and start `python -m app.sample_worker`.
4. The run detail page shows a `wait_reason` per step (`waiting_for_worker`, `rate_limited`, `concurrency_limited`, `circuit_open`, `approval_pending`).

### Worker unhealthy / lease expirations climbing
- `orchestrator_lease_expirations_total` rising means workers claim tasks but never heartbeat or complete. Check worker logs, CPU, and network to the API.
- Steps that exceed `poison_task_expiry_limit` lease expirations land in the DLQ (`/api/v1/dlq`) as failed. Redrive from the DLQ page after fixing the cause.

### DLQ growing
- Inspect the step error in the DLQ list. Common causes: connector misconfiguration (bad URL/credentials), missing `{"$connection": ...}` values, task-type bugs.
- Fix the cause, then redrive (`POST /api/v1/dlq/{step_run_id}/redrive` — idempotent).

### Notifications failing
- `orchestrator_notification_deliveries_total{status="failed"}` and the delivery log (`GET /api/v1/notifications/deliveries`) show per-channel errors.
- Webhook channels: check the receiver's status codes. Slack channels: the webhook URL must be `hooks.slack.com`. Email channels: `SMTP_HOST` must be configured.

### Webhook triggers rejected (429)
- `POST /api/v1/hooks/{trigger_id}` is rate-limited to 60/min per trigger+IP (`trigger_rate_limited`). Slow the sender or rotate to a new trigger.

### Storage quota exceeded on upload
- `quota_storage_bytes_per_user` (default 1 GiB). Users can delete old documents (`DELETE /api/v1/documents/{id}`) or the operator can raise the quota.

### Secrets key rotation
```
python -m app.cli rotate-secrets-key --dry-run   # verify decrypt works
python -m app.cli rotate-secrets-key             # prints the new key
```
Covers workflow secrets, trigger signing secrets, notification channel targets and connection values. Store the printed key as `SECRETS_ENCRYPTION_KEY` and restart.

## Key endpoints
- Health: `GET /ready`, `GET /metrics` (Prometheus), `GET /api/v1/ops/overview`, `GET /api/v1/ops/capacity`
- Ops: `GET /api/v1/dlq`, `GET /api/v1/notifications/deliveries`, `GET /api/v1/audit` (admin)
