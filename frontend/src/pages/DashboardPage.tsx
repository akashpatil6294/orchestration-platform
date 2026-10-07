/**
 * Dashboard presentation over the existing live resource and action handlers.
 * Summary, runs, workers and metrics share the same API snapshot and time range.
 */
import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { api } from '../lib/api'
import type { DashboardResponse, NeedsAttentionItem, Paginated, RunDetail, WorkflowSummary } from '../lib/types'
import { useLiveResource } from '../lib/useLiveResource'
import { useConfirm, type ConfirmOptions } from '../components/ConfirmDialogProvider'
import { useToast } from '../components/ToastProvider'
import { ActionButton } from '../components/ActionButton'
import { WorkerCapacityBanner } from '../components/WorkerCapacityBanner'
import QuotaMeters from '../components/QuotaMeters'
import { AlertBar, DashboardHeader, DeadLetterIndicator, MetricsPanel, RunsTable, SummaryBand, WorkersPanel } from '../components/DashboardSections'
import {
  EmptyState,
  ErrorState,
  InlineLoader,
  StatusBadge,
  formatRelative,
} from '../components/StatusView'

const REFRESH_STORAGE_KEY = 'orchestrator.dashboard.refresh'
const WINDOW_STORAGE_KEY = 'orchestrator.dashboard.window'

const REFRESH_CHOICES: { label: string; ms: number }[] = [
  { label: 'Off', ms: 0 },
  { label: '5s', ms: 5000 },
  { label: '10s', ms: 10000 },
  { label: '30s', ms: 30000 },
  { label: '60s', ms: 60000 },
]

const WINDOW_CHOICES: { label: string; hours: number }[] = [
  { label: '1h', hours: 1 },
  { label: '24h', hours: 24 },
  { label: '7d', hours: 168 },
  { label: '30d', hours: 720 },
]

const ACTIVE_RUN_STATUSES = new Set(['queued', 'running', 'cancelling'])

function readStoredNumber(key: string, fallback: number, allowed: number[]): number {
  try {
    const raw = window.localStorage.getItem(key)
    const value = raw === null ? Number.NaN : Number(raw)
    return allowed.includes(value) ? value : fallback
  } catch {
    return fallback
  }
}

function writeStoredNumber(key: string, value: number) {
  try {
    window.localStorage.setItem(key, String(value))
  } catch {
    // Preferences are best-effort; the UI works without persistence.
  }
}

function Segmented<T extends number>({
  choices,
  value,
  onChange,
  ariaLabel,
}: {
  choices: { label: string; value: T }[]
  value: T
  onChange: (value: T) => void
  ariaLabel: string
}) {
  return (
    <div className="segmented" role="group" aria-label={ariaLabel}>
      {choices.map((choice) => (
        <button key={choice.value} type="button" aria-pressed={choice.value === value} onClick={() => onChange(choice.value)}>
          {choice.label}
        </button>
      ))}
    </div>
  )
}

type ActivityFilter = 'all' | 'failures' | 'approvals' | 'scheduling'

function activityMatches(filter: ActivityFilter, item: { kind: string; status: string; label: string }): boolean {
  if (filter === 'all') return true
  if (filter === 'failures') return item.status.includes('fail') || item.label.toLowerCase().includes('fail')
  if (filter === 'approvals') return item.label.toLowerCase().includes('approval') || item.status.startsWith('approval')
  return item.kind === 'event' && item.label.toLowerCase().includes('schedule')
}

function StatusBreakdown({ counts }: { counts: { status: string; count: number }[] }) {
  const visible = counts.filter((entry) => entry.count > 0)
  if (visible.length === 0) {
    return <p className="muted">No runs in this window yet.</p>
  }
  const total = visible.reduce((sum, entry) => sum + entry.count, 0)
  return (
    <ul className="status-breakdown">
      {visible.map((entry) => (
        <li key={entry.status}>
          <Link to={`/runs?status=${entry.status}`} className="row-actions" aria-label={`Show ${entry.status} runs`}>
            <StatusBadge status={entry.status} />
          </Link>
          <span className="status-breakdown-count">{entry.count}</span>
          <span className="status-breakdown-share">{Math.round((entry.count / total) * 100)}%</span>
        </li>
      ))}
    </ul>
  )
}

export function DashboardPage() {
  const toast = useToast()
  const confirmAction = useConfirm()

  const [refreshMs, setRefreshMs] = useState(() => readStoredNumber(REFRESH_STORAGE_KEY, 10000, REFRESH_CHOICES.map((choice) => choice.ms)))
  const [windowHours, setWindowHours] = useState(() => readStoredNumber(WINDOW_STORAGE_KEY, 24, WINDOW_CHOICES.map((choice) => choice.hours)))
  const [activityFilter, setActivityFilter] = useState<ActivityFilter>('all')
  const [runPanelOpen, setRunPanelOpen] = useState(false)

  useEffect(() => {
    writeStoredNumber(REFRESH_STORAGE_KEY, refreshMs)
  }, [refreshMs])

  useEffect(() => {
    writeStoredNumber(WINDOW_STORAGE_KEY, windowHours)
  }, [windowHours])

  const dashboard = useLiveResource<DashboardResponse>(
    (signal) => api.get<DashboardResponse>(`/api/v1/runs/dashboard?window_hours=${windowHours}`, signal),
    [windowHours],
    refreshMs,
    refreshMs > 0,
  )

  const { data } = dashboard

  const updateRefresh = useCallback(
    (value: number) => {
      setRefreshMs(value)
      if (value === 0) dashboard.refresh()
    },
    [dashboard],
  )

  const afterAction = useCallback(
    (message: string) => {
      toast.success(message)
      dashboard.refresh()
    },
    [dashboard, toast],
  )

  const runAction = useCallback(
    async (path: string, body: unknown, confirmOptions: ConfirmOptions, successMessage: string) => {
      const ok = await confirmAction.confirm(confirmOptions)
      if (!ok) return false
      await api.post(path, body)
      afterAction(successMessage)
      return true
    },
    [afterAction, confirmAction],
  )

  const rerunFromStep = useCallback(
    async (runId: string) => {
      const detail = await api.get<RunDetail>(`/api/v1/runs/${runId}`)
      const stepKey = detail.retryable_steps[0]
      if (!stepKey) {
        toast.info('Nothing to re-run', 'This run has no failed steps to re-run from.')
        return
      }
      const ok = await confirmAction.confirm({
        title: 'Re-run from step',
        message: `Re-run "${stepKey}" and every step after it in this run? Results of the later steps are discarded.`,
        confirmLabel: 'Re-run',
      })
      if (!ok) return
      await api.post(`/api/v1/runs/${runId}/rerun-from-step`, { steps: [stepKey], reset_downstream: true })
      afterAction(`Re-running from "${stepKey}"`)
    },
    [afterAction, confirmAction, toast],
  )

  const decideApproval = useCallback(
    async (item: NeedsAttentionItem, decision: 'approve' | 'reject') => {
      if (decision === 'reject') {
        const ok = await confirmAction.confirm({
          title: 'Reject approval',
          message: `Reject step "${item.step_key}"? The run will follow its rejection policy.`,
          confirmLabel: 'Reject',
          danger: true,
        })
        if (!ok) return
      }
      await api.post(`/api/v1/runs/${item.run_id}/steps/${item.step_key}/approval`, { decision })
      afterAction(decision === 'approve' ? 'Step approved' : 'Step rejected')
    },
    [afterAction, confirmAction],
  )

  if (dashboard.initial) return <InlineLoader label="Loading your dashboard…" />
  if (dashboard.error && !data) return <ErrorState message={dashboard.error} onRetry={dashboard.refresh} />

  if (!data) {
    return (
      <EmptyState
        title="Nothing to show yet"
        description="Once a workflow has published a version and started a run, it will appear here."
        action={
          <Link className="button button-primary" to="/workflows">
            Go to workflows
          </Link>
        }
      />
    )
  }

  const { stats } = data
  const activity = (data.recent_activity ?? []).filter((item) => activityMatches(activityFilter, item))
  const attention = data.needs_attention ?? []

  const updatedLabel = dashboard.hidden
    ? 'Paused (tab hidden)'
    : dashboard.secondsAgo === null
      ? 'Updated just now'
      : `Updated ${dashboard.secondsAgo}s ago`

  return (
    <div className="page dashboard-page">
      <DashboardHeader>
        <h1>Dashboard</h1>
        <Segmented ariaLabel="Time window" choices={WINDOW_CHOICES.map(choice => ({ label: choice.label, value: choice.hours }))} value={windowHours} onChange={setWindowHours}/>
        <ActionButton label="Run demo" pendingLabel="Seeding…" errorTitle="Demo seeding failed" onAction={async () => { await api.post('/api/v1/demo/install'); afterAction('Demo workflows installed') }}/>
        <Link to="/settings" className="button button-ghost" aria-label="Create API token">API tokens</Link>
        <Link to="/workflows" className="button button-primary">New workflow</Link>
      </DashboardHeader>
      <DeadLetterIndicator present={attention.some(item => item.kind === 'dlq')}/>
      <AlertBar workers={data.workers}/>
      <div className="dashboard-toolbar">
        <div className="live-controls"><span className="updated-ago" aria-live="polite">{updatedLabel}</span>
          <Segmented ariaLabel="Auto refresh" choices={REFRESH_CHOICES.map(choice => ({ label: choice.label, value: choice.ms }))} value={refreshMs} onChange={updateRefresh}/>
          <button type="button" className="button button-ghost" onClick={dashboard.refresh} disabled={dashboard.loading}>{dashboard.loading ? 'Refreshing…' : 'Refresh'}</button>
        </div>
        <button type="button" className="button button-secondary" aria-expanded={runPanelOpen} onClick={() => setRunPanelOpen(open => !open)}>{runPanelOpen ? 'Close run panel' : 'Run workflow'}</button>
      </div>
      {dashboard.error && <div role="alert" className="alert alert-error">{dashboard.error} · Showing the last available data.</div>}
      <SummaryBand stats={stats} range={WINDOW_CHOICES.find(choice => choice.hours === windowHours)?.label || `${windowHours}h`}/>
      <div className="dashboard-main-grid">
        <RunsTable runs={data.recent_runs} renderActions={run => {
          const active = ACTIVE_RUN_STATUSES.has(run.status)
          const hasFailures = (run.step_counts?.failed ?? 0) > 0
          return <>
                          {active ? (
                            <ActionButton
                              label="Cancel"
                              variant="danger"
                              errorTitle="Cancel failed"
                              onAction={() =>
                                runAction(
                                  `/api/v1/runs/${run.id}/cancel`,
                                  undefined,
                                  { title: 'Cancel run', message: `Cancel the ${run.workflow_name || 'workflow'} run? Running steps are asked to stop.`, confirmLabel: 'Cancel run', danger: true },
                                  'Cancellation requested',
                                )
                              }
                            />
                          ) : null}
                          {run.status === 'paused' ? (
                            <ActionButton
                              label="Resume"
                              errorTitle="Resume failed"
                              onAction={async () => {
                                await api.post(`/api/v1/runs/${run.id}/resume`)
                                afterAction('Run resumed')
                              }}
                            />
                          ) : active ? (
                            <ActionButton
                              label="Pause"
                              errorTitle="Pause failed"
                              onAction={async () => {
                                await api.post(`/api/v1/runs/${run.id}/pause`)
                                afterAction('Run paused')
                              }}
                            />
                          ) : null}
                          {hasFailures ? (
                            <ActionButton
                              label="Retry failed"
                              errorTitle="Retry failed"
                              onAction={() =>
                                runAction(
                                  `/api/v1/runs/${run.id}/retry`,
                                  { steps: [], reset_downstream: true },
                                  { title: 'Retry failed steps', message: `Retry every failed step in this run? Downstream steps re-run too.`, confirmLabel: 'Retry' },
                                  'Retry scheduled',
                                )
                              }
                            />
                          ) : null}
                          {hasFailures ? (
                            <ActionButton label="Re-run from step" errorTitle="Re-run failed" onAction={() => rerunFromStep(run.id)} />
                          ) : null}
                          <Link className="button button-ghost" to={`/workflows/${run.workflow_id}`}>
                            Open workflow
                          </Link>

          </>
        }}/>
        <WorkersPanel workers={data.workers} stats={stats}/>
      </div>
      <MetricsPanel data={data}/>
      <div className="dashboard-secondary-stats">
        <Link to="/workflows">{stats.workflows_total} workflows</Link>
        <Link to="/runs">{stats.retried_steps} retried steps · {stats.step_attempts} attempts</Link>
        <Link to="/runs?status=failed">{stats.failed_steps} failed steps · {stats.timed_out_steps} timed out</Link>
        <Link to="/ops/workers">{stats.running_steps} running steps</Link>
        <Link to="/schedules">{stats.schedules_due} schedules due</Link>
      </div>
      <QuotaMeters/>
      {runPanelOpen ? <RunWorkflowPanel onStarted={() => afterAction('Run started')} /> : null}

      <WorkerCapacityBanner />

      {attention.length > 0 ? (
        <section className="panel" aria-label="Needs attention">
          <div className="panel-head">
            <h2>Needs attention</h2>
          </div>
          <ul className="attention-list">
            {attention.slice(0, 8).map((item) => (
              <li key={item.kind + item.id} className="attention-item" data-severity={item.severity}>
                <span className="attention-label">{item.label}</span>
                <span className="attention-detail">{item.detail}</span>
                <span className="row-actions">
                  {item.kind === 'approval' && item.run_id && item.step_key ? (
                    <>
                      <ActionButton
                        label="Approve"
                        variant="primary"
                        errorTitle="Approval failed"
                        onAction={() => decideApproval(item, 'approve')}
                      />
                      <ActionButton label="Reject" variant="danger" errorTitle="Rejection failed" onAction={() => decideApproval(item, 'reject')} />
                    </>
                  ) : null}
                  {item.run_id ? (
                    <Link className="button button-ghost" to={`/runs/${item.run_id}`}>
                      Open run
                    </Link>
                  ) : item.kind === 'worker_stale' ? (
                    <Link className="button button-ghost" to="/ops/workers">
                      Inspect workers
                    </Link>
                  ) : item.kind === 'dlq' ? (
                    <Link className="button button-ghost" to="/ops/dlq">
                      Open dead letters
                    </Link>
                  ) : item.kind.startsWith('schedule') ? (
                    <Link className="button button-ghost" to="/schedules">
                      Manage schedules
                    </Link>
                  ) : null}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <div className="two-column dashboard-secondary-grid">
        <section className="panel"><div className="panel-head"><h2>Runs by status</h2></div><StatusBreakdown counts={stats.runs_by_status}/></section>
        <section className="panel">
          <div className="panel-head">
            <h2>Recent activity</h2>
            <div className="chip-row" role="group" aria-label="Activity filter">
              {(
                [
                  ['all', 'All'],
                  ['failures', 'Failures'],
                  ['approvals', 'Approvals'],
                  ['scheduling', 'Scheduling'],
                ] as [ActivityFilter, string][]
              ).map(([value, label]) => (
                <button key={value} type="button" className="chip" aria-pressed={activityFilter === value} onClick={() => setActivityFilter(value)}>
                  {label}
                </button>
              ))}
            </div>
          </div>
          {activity.length === 0 ? (
            <p className="muted">No events recorded yet.</p>
          ) : (
            <ol className="activity-list">
              {activity.map((item) => {
                const target = item.run_id ? `/runs/${item.run_id}` : item.workflow_id ? `/workflows/${item.workflow_id}` : null
                return (
                  <li
                    key={item.id}
                    className={`activity${item.status.endsWith('failed') ? ' activity-error' : item.status.endsWith('skipped') ? ' activity-warning' : ''}`}
                  >
                    <span className="activity-type">
                      {target ? (
                        <Link to={target}>{item.label}</Link>
                      ) : (
                        item.label
                      )}
                    </span>
                    <span className="activity-time">{formatRelative(item.at)}</span>
                    <span className="activity-message">{item.detail}</span>
                  </li>
                )
              })}
            </ol>
          )}
        </section>
      </div>
    </div>
  )
}

function RunWorkflowPanel({ onStarted }: { onStarted: () => void }) {
  const navigate = useNavigate()
  const toast = useToast()
  const [workflows, setWorkflows] = useState<WorkflowSummary[] | null>(null)
  const [workflowId, setWorkflowId] = useState('')
  const [inputText, setInputText] = useState('{}')
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    api
      .get<Paginated<WorkflowSummary>>('/api/v1/workflows?limit=50', controller.signal)
      .then((page) => {
        const runnable = page.items.filter((workflow) => workflow.latest_version > 0 && !workflow.archived)
        setWorkflows(runnable)
        if (runnable.length > 0) setWorkflowId(runnable[0].id)
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === 'AbortError') return
        setError(cause instanceof Error ? cause.message : 'Could not load workflows')
      })
    return () => controller.abort()
  }, [])

  const start = async () => {
    setError(null)
    let input: unknown
    try {
      input = JSON.parse(inputText || '{}')
    } catch {
      setError('Input must be valid JSON, for example {"email": "ops@example.com"}.')
      return
    }
    if (input === null || typeof input !== 'object' || Array.isArray(input)) {
      setError('Input must be a JSON object.')
      return
    }
    if (!workflowId) {
      setError('Pick a published workflow to run.')
      return
    }
    setStarting(true)
    try {
      const run = await api.post<{ id: string }>(`/api/v1/workflows/${workflowId}/runs`, { input })
      onStarted()
      navigate(`/runs/${run.id}`)
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : 'Could not start the run'
      setError(message)
      toast.error('Run failed to start', message)
    } finally {
      setStarting(false)
    }
  }

  return (
    <section className="panel" aria-label="Run a workflow">
      <h2>Run a workflow</h2>
      {error ? (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      ) : null}
      {workflows === null ? (
        <InlineLoader label="Loading workflows…" />
      ) : workflows.length === 0 ? (
        <p className="muted">No published workflows yet — create and publish one first.</p>
      ) : (
        <div className="form-grid">
          <label className="field">
            <span>Workflow</span>
            <select className="input" value={workflowId} onChange={(event) => setWorkflowId(event.target.value)}>
              {workflows.map((workflow) => (
                <option key={workflow.id} value={workflow.id}>
                  {workflow.name} (v{workflow.latest_version})
                </option>
              ))}
            </select>
          </label>
          <label className="field field-wide">
            <span>Input (JSON object)</span>
            <textarea
              className="input textarea"
              rows={4}
              value={inputText}
              onChange={(event) => setInputText(event.target.value)}
              aria-label="Run input as JSON"
              spellCheck={false}
            />
          </label>
          <div className="form-actions">
            <button type="button" className="button button-primary" onClick={start} disabled={starting}>
              {starting ? 'Starting…' : 'Start run'}
            </button>
          </div>
        </div>
      )}
    </section>
  )
}

export default DashboardPage
