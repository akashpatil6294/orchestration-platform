/**
 * Types mirroring the FastAPI response models.
 *
 * Kept hand-written and small: the API is the contract, and these describe only
 * the fields the interface actually renders.
 */

export interface AppUser {
  id: string
  email: string
  display_name: string
  is_admin: boolean
  created_at: string
  initials: string
  avatar_url?: string | null
  auth_provider?: string
  has_password?: boolean
}

export interface SessionInfo {
  user: AppUser
  permissions: string[]
  environment: string
  features: Record<string, unknown>
  sign_in_method: string
}

export interface WorkflowSummary {
  id: string
  name: string
  description: string
  latest_version: number
  step_count: number
  archived: boolean
  default_max_parallel: number
  created_at: string
  updated_at: string
  run_counts: Record<string, number>
  last_run_at: string | null
  last_run_status: string | null
  has_draft_changes: boolean
  schedule_count?: number
}

export interface WorkflowStep {
  id: string
  type: string
  name?: string
  description?: string
  input: Record<string, unknown>
  depends_on: string[]
  retries: number
  timeout_seconds: number
  backoff_seconds?: number
  backoff_multiplier?: number
  required?: boolean
  continue_on_error?: boolean
}

export interface WorkflowDefinition {
  name: string
  description: string
  steps: WorkflowStep[]
  default_max_parallel: number
  tags: string[]
}

export interface WorkflowVersionSummary {
  version: number
  published_at: string
  published_by: string | null
  publish_note: string
  step_count: number
  definition_hash: string
  is_latest: boolean
}

export interface WorkflowVersionDetail extends WorkflowVersionSummary {
  definition: WorkflowDefinition
}

export interface WorkflowDetail extends WorkflowSummary {
  draft: WorkflowDefinition
  versions: WorkflowVersionSummary[]
  team_id?: string | null
  user_role?: string | null
}

export interface RunSummary {
  id: string
  workflow_id: string
  parent_run_id?: string | null
  nesting_depth?: number
  logical_date?: string | null
  interval_start?: string | null
  interval_end?: string | null
  workflow_name: string
  version: number
  status: string
  trigger: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  duration_seconds: number | null
  step_counts: Record<string, number>
  total_steps: number
  completed_steps: number
  progress: number
  cancel_requested: boolean
  idempotent_replay?: boolean
}

export interface StepRun {
  id: string
  key: string
  name: string
  type: string
  status: string
  queue: string
  depends_on: string[]
  attempts: number
  retry_limit: number
  required: boolean
  timeout_seconds: number
  available_at: string | null
  deadline_at: string | null
  started_at: string | null
  finished_at: string | null
  duration_seconds: number | null
  worker_id: string | null
  output: unknown
  error: Record<string, unknown> | null
  input: Record<string, unknown>
  log_lines: number
  last_log_at: string | null
  downstream: string[]
  retry_at: string | null
  spec: Record<string, unknown>
  parent_step_id: string | null
  child_run_id: string | null
  foreach_index: number | null
  wait_reason: string | null
}

export interface RunDetail extends RunSummary {
  input: Record<string, unknown>
  output: unknown
  error: Record<string, unknown> | null
  cancel_requested_at: string | null
  max_parallel: number
  deadline_at: string | null
  sla_deadline_at: string | null
  sla_breached_at: string | null
  paused_at: string | null
  logical_date?: string | null
  interval_start?: string | null
  interval_end?: string | null
  steps: StepRun[]
  latest_event_seq: number
  retryable_steps: string[]
}

export interface RunEvent {
  seq: number
  id: string
  type: string
  level: string
  message: string
  step_key: string | null
  actor: string | null
  payload: Record<string, unknown>
  created_at: string
}

export interface RunEventsResponse {
  items: RunEvent[]
  latest_seq: number
  has_more: boolean
}

export interface ScheduleView {
  id: string
  workflow_id: string
  workflow_name: string
  name: string
  cron_expression: string
  cron_description: string
  timezone: string
  enabled: boolean
  version: number | null
  effective_version: number
  input: Record<string, unknown>
  overlap_policy: string
  catchup: boolean
  data_interval_seconds: number | null
  jitter_seconds: number
  skip_weekends: boolean
  skip_dates: string[]
  pause_windows: { start: string; end: string }[]
  next_run_at: string | null
  last_run_at: string | null
  last_run_id: string | null
  last_status: string | null
  run_count: number
  last_error: string | null
  upcoming: string[]
  created_at: string
  updated_at: string
}

export interface WorkflowTrigger {
  id: string
  workflow_id: string
  workflow_name: string
  kind: 'webhook' | 'workflow_success'
  name: string
  source_workflow_id: string | null
  version: number | null
  input_mapping: Record<string, string>
  enabled: boolean
  rate_limit_per_minute: number
  endpoint: string | null
  created_at: string
  updated_at: string
}

export interface ScheduleBackfill {
  id: string
  schedule_id: string
  start: string
  end: string
  next_slot_at: string | null
  concurrency_limit: number
  status: string
  created_at: string
  finished_at: string | null
}

export interface DashboardStats {
  window_hours: number
  runs_total: number
  runs_by_status: { status: string; count: number }[]
  runs_started: number
  runs_finished: number
  success_rate: number
  avg_duration_seconds: number | null
  p95_duration_seconds: number | null
  step_attempts: number
  retried_steps: number
  failed_steps: number
  timed_out_steps: number
  active_workers: number
  total_workers: number
  pending_steps: number
  running_steps: number
  schedules_enabled: number
  schedules_due: number
  workflows_total: number
  latest_event_at: string | null
}

export interface WorkerLoad {
  worker_id: string
  name: string
  active: boolean
  stale: boolean
  task_types: string[]
  max_concurrency: number
  running_steps: number
  last_seen_at: string | null
}

export interface DashboardResponse {
  stats: DashboardStats
  recent_runs: RunSummary[]
  recent_activity: {
    kind: string
    id: string
    run_id: string | null
    workflow_id: string | null
    workflow_name: string
    status: string
    label: string
    detail: string
    at: string
  }[]
  workers: WorkerLoad[]
  top_workflows: Record<string, unknown>[]
  needs_attention?: NeedsAttentionItem[]
  runs_timeline?: TimelineBucket[]
  duration_trend?: DurationBucket[]
}

export interface NeedsAttentionItem {
  kind: string
  id: string
  run_id: string | null
  workflow_id: string | null
  step_key?: string | null
  severity: string
  label: string
  detail: string
  at: string
}

export interface TimelineBucket {
  bucket_start: string
  label: string
  started: number
  succeeded: number
  failed: number
}

export interface DurationBucket {
  bucket_start: string
  label: string
  avg_duration_seconds: number | null
}

export interface ApiToken {
  id: string
  name: string
  token_prefix: string
  scopes: string[]
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
}

export interface ApiTokenCreated extends ApiToken {
  /** Returned exactly once, immediately after creation. */
  token: string
}

export interface Paginated<T> {
  items: T[]
  total: number
  limit: number
  offset: number
  has_more: boolean
}

export interface DlqEntry {
  step_run_id: string
  run_id: string
  workflow_id: string
  workflow_name: string
  step_key: string
  task_type: string
  attempts: number
  retry_limit: number
  lease_expirations: number
  priority: number
  queue: string
  error: Record<string, unknown> | null
  failed_at: string | null
}

export interface RedriveResult {
  run_id: string
  step_run_id: string
  status: string
  already_redriven: boolean
  retried_steps: string[]
  reset_steps: string[]
}

export interface WorkerView {
  worker_id: string
  name: string
  active: boolean
  stale: boolean
  task_types: string[]
  queues: string[]
  max_concurrency: number
  last_seen_at: string
  created_at: string
  running_steps: number
}

export interface WorkerTaskItem {
  step_run_id: string
  run_id: string
  step_key: string
  task_type: string
  attempt: number
  deadline_at: string | null
  started_at: string | null
}

export interface OpsWorkerSummary {
  total: number
  active: number
  stale: number
  task_type_coverage: Record<string, number>
  items: WorkerView[]
}

export interface OpsOverview {
  environment: string
  version: string
  server_time: string
  configuration: Record<string, unknown>
  database: string
  dispatch: {
    backend: string
    redis_configured: boolean
    relay_running: boolean
    outbox_backlog: number
  }
  scheduler: {
    enabled: boolean
    loop_running: boolean
    interval_seconds: number
    last_tick_at: string | null
    last_error: string | null
    enabled_schedules: number
    due_schedules: number
  }
  workers: OpsWorkerSummary
  runs: { by_status: Record<string, number>; total: number }
  steps: { by_status: Record<string, number>; total: number }
  recent_failures: {
    seq: number
    run_id: string | null
    step_key: string | null
    type: string
    level: string
    message: string
    at: string
  }[]
}

export interface WorkerTokenCreated {
  worker_id: string
  token: string
  token_prefix: string
  task_types: string[]
  created_at: string
}

export interface WorkflowSecret {
  name: string
  created_at: string
  updated_at: string
}
