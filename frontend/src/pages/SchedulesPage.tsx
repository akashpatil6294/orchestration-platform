import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { ApiError, api } from '../lib/api'
import type { Paginated, ScheduleBackfill, ScheduleView, WorkflowSummary } from '../lib/types'
import { useResource } from '../lib/useResource'
import { useConfirm } from '../components/ConfirmDialogProvider'
import { useToast } from '../components/ToastProvider'
import {
  EmptyState,
  ErrorState,
  InlineLoader,
  StatusBadge,
  formatRelative,
  formatTimestamp,
} from '../components/StatusView'

interface CronPreview {
  valid: boolean
  error: string | null
  description: string
  timezone: string
  next_runs: string[]
}

export function SchedulesPage() {
  const [params, setParams] = useSearchParams()
  const confirmAction = useConfirm()
  const toast = useToast()
  const workflowFilter = params.get('workflow_id') ?? ''

  const schedules = useResource<{ items: ScheduleView[]; total: number }>(
    (signal) =>
      api.get<{ items: ScheduleView[]; total: number }>(
        `/api/v1/schedules${workflowFilter ? `?workflow_id=${workflowFilter}` : ''}`,
        signal,
      ),
    [workflowFilter],
  )
  const workflows = useResource<Paginated<WorkflowSummary>>(
    (signal) => api.get<Paginated<WorkflowSummary>>('/api/v1/workflows', signal),
    [],
  )

  const [workflowId, setWorkflowId] = useState(workflowFilter)
  const [cron, setCron] = useState('0 7 * * *')
  const [timezone, setTimezone] = useState('UTC')
  const [name, setName] = useState('')
  const [dataIntervalSeconds, setDataIntervalSeconds] = useState('')
  const [jitterSeconds, setJitterSeconds] = useState('0')
  const [skipWeekends, setSkipWeekends] = useState(false)
  const [skipDatesText, setSkipDatesText] = useState('')
  const [pauseWindowsText, setPauseWindowsText] = useState('[]')
  const [backfillFor, setBackfillFor] = useState<string | null>(null)
  const [backfillStart, setBackfillStart] = useState(() => new Date(Date.now() - 7 * 86400000).toISOString().slice(0, 10))
  const [backfillEnd, setBackfillEnd] = useState(() => new Date().toISOString().slice(0, 10))
  const [backfillConcurrency, setBackfillConcurrency] = useState('2')
  const [backfills, setBackfills] = useState<Record<string, ScheduleBackfill[]>>({})
  const [preview, setPreview] = useState<CronPreview | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const publishableWorkflows = (workflows.data?.items ?? []).filter((workflow) => workflow.latest_version > 0)

  async function previewCron() {
    setActionError(null)
    try {
      setPreview(await api.post<CronPreview>('/api/v1/schedules/preview', { cron_expression: cron, timezone, count: 5 }))
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The cron expression could not be checked.')
    }
  }

  async function createSchedule() {
    if (!workflowId) {
      setActionError('Choose a published workflow first.')
      return
    }
    let pauseWindows: { start: string; end: string }[]
    try {
      const parsed: unknown = JSON.parse(pauseWindowsText)
      if (!Array.isArray(parsed) || parsed.some((item) => !item || typeof item.start !== 'string' || typeof item.end !== 'string')) {
        throw new Error('Use a JSON array of {"start":"ISO date/time","end":"ISO date/time"} objects.')
      }
      pauseWindows = parsed as { start: string; end: string }[]
    } catch (cause) {
      setActionError(cause instanceof Error ? `Pause windows: ${cause.message}` : 'Pause windows must be valid JSON.')
      return
    }
    setBusy('create')
    setActionError(null)
    try {
      await api.post<ScheduleView>(`/api/v1/workflows/${workflowId}/schedules`, {
        name: name.trim(),
        cron_expression: cron,
        timezone,
        enabled: true,
        ...(dataIntervalSeconds ? { data_interval_seconds: Number(dataIntervalSeconds) } : {}),
        jitter_seconds: Number(jitterSeconds) || 0,
        skip_weekends: skipWeekends,
        skip_dates: skipDatesText.split(',').map((value) => value.trim()).filter(Boolean),
        pause_windows: pauseWindows,
      })
      setName('')
      setPreview(null)
      schedules.reload()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The schedule could not be created.')
    } finally {
      setBusy(null)
    }
  }

  async function toggleSchedule(schedule: ScheduleView) {
    setBusy(schedule.id)
    setActionError(null)
    try {
      await api.post(`/api/v1/schedules/${schedule.id}/toggle`, { enabled: !schedule.enabled })
      schedules.reload()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The schedule could not be updated.')
    } finally {
      setBusy(null)
    }
  }

  async function runNow(schedule: ScheduleView) {
    setBusy(schedule.id)
    setActionError(null)
    try {
      await api.post(`/api/v1/schedules/${schedule.id}/run-now`)
      schedules.reload()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The schedule could not be triggered.')
    } finally {
      setBusy(null)
    }
  }

  async function removeSchedule(schedule: ScheduleView) {
    const ok = await confirmAction.confirm({
      title: 'Delete schedule',
      message: `Delete the schedule “${schedule.name || schedule.cron_expression}”? Pending backfills keep running, but no new runs are scheduled.`,
      confirmLabel: 'Delete schedule',
      danger: true,
    })
    if (!ok) return
    setBusy(schedule.id)
    setActionError(null)
    try {
      await api.remove(`/api/v1/schedules/${schedule.id}`)
      toast.success('Schedule deleted')
      schedules.reload()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The schedule could not be deleted.')
    } finally {
      setBusy(null)
    }
  }

  async function loadBackfills(scheduleId: string) {
    try {
      const result = await api.get<{ items: ScheduleBackfill[] }>(`/api/v1/schedules/${scheduleId}/backfills`)
      setBackfills((current) => ({ ...current, [scheduleId]: result.items }))
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'Backfill jobs could not be loaded.')
    }
  }

  async function createBackfill(schedule: ScheduleView) {
    if (!backfillStart || !backfillEnd) {
      setActionError('Choose both a start and end date for the backfill.')
      return
    }
    const endExclusive = new Date(`${backfillEnd}T00:00:00.000Z`)
    endExclusive.setUTCDate(endExclusive.getUTCDate() + 1)
    setBusy(`backfill:${schedule.id}`)
    setActionError(null)
    try {
      await api.post<ScheduleBackfill>(`/api/v1/schedules/${schedule.id}/backfill`, {
        start: `${backfillStart}T00:00:00.000Z`,
        end: endExclusive.toISOString(),
        concurrency_limit: Number(backfillConcurrency),
      })
      await loadBackfills(schedule.id)
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The backfill could not be started.')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>Schedules</h1>
          <p className="page-subtitle">Timezone-aware cron cadence with duplicate-run protection.</p>
        </div>
        {workflowFilter ? (
          <button
            type="button"
            className="button button-secondary"
            onClick={() => {
              setParams({})
              setWorkflowId('')
            }}
          >
            Clear workflow filter
          </button>
        ) : null}
      </header>

      {actionError ? (
        <div className="alert alert-error" role="alert">
          {actionError}
        </div>
      ) : null}

      <section className="panel">
        <h2>Add a schedule</h2>
        {workflows.loading && !workflows.data ? (
          <InlineLoader label="Loading workflows…" />
        ) : publishableWorkflows.length === 0 ? (
          <EmptyState
            title="No published workflows"
            description="A schedule always targets a published version. Publish a workflow first."
            action={
              <Link className="button button-secondary" to="/workflows">
                Go to workflows
              </Link>
            }
          />
        ) : (
          <div className="form-grid">
            <label className="field">
              <span>Workflow</span>
              <select className="input" value={workflowId} onChange={(event) => setWorkflowId(event.target.value)}>
                <option value="">Select a workflow</option>
                {publishableWorkflows.map((workflow) => (
                  <option key={workflow.id} value={workflow.id}>
                    {workflow.name} (v{workflow.latest_version})
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>Data interval (seconds, optional)</span>
              <input className="input" type="number" min="1" max="31536000" value={dataIntervalSeconds} onChange={(event) => setDataIntervalSeconds(event.target.value)} />
            </label>
            <label className="field">
              <span>Jitter (seconds)</span>
              <input className="input" type="number" min="0" max="3600" value={jitterSeconds} onChange={(event) => setJitterSeconds(event.target.value)} />
            </label>
            <label className="field">
              <span>Skip dates (YYYY-MM-DD, comma separated)</span>
              <input className="input" value={skipDatesText} onChange={(event) => setSkipDatesText(event.target.value)} placeholder="2026-12-25, 2027-01-01" />
            </label>
            <label className="field field-checkbox">
              <input type="checkbox" checked={skipWeekends} onChange={(event) => setSkipWeekends(event.target.checked)} />
              <span>Skip weekends</span>
            </label>
            <label className="field field-wide">
              <span>Pause windows (schedule timezone)</span>
              <textarea className="input" rows={2} value={pauseWindowsText} onChange={(event) => setPauseWindowsText(event.target.value)} aria-label="Pause windows" />
              <small>Use ISO 8601 date/time ranges in the schedule timezone, for example <code>{'[{"start":"2026-12-24T00:00:00","end":"2026-12-27T00:00:00"}]'}</code>.</small>
            </label>
            <label className="field">
              <span>Name (optional)</span>
              <input className="input" value={name} onChange={(event) => setName(event.target.value)} />
            </label>
            <label className="field">
              <span>Cron expression</span>
              <input
                className="input"
                value={cron}
                onChange={(event) => {
                  setCron(event.target.value)
                  setPreview(null)
                }}
              />
            </label>
            <label className="field">
              <span>Timezone</span>
              <input
                className="input"
                value={timezone}
                onChange={(event) => {
                  setTimezone(event.target.value)
                  setPreview(null)
                }}
              />
            </label>
            <div className="form-actions">
              <button type="button" className="button button-secondary" onClick={() => void previewCron()}>
                Preview next runs
              </button>
              <button type="button" className="button button-primary" onClick={createSchedule} disabled={busy === 'create'}>
                {busy === 'create' ? 'Creating…' : 'Create schedule'}
              </button>
            </div>
          </div>
        )}

        {preview ? (
          preview.valid ? (
            <div className="alert alert-success" role="status">
              <strong>{preview.description || cron}</strong>
              <span>
                Next runs: {preview.next_runs.map((entry) => formatTimestamp(entry)).join(', ')}
              </span>
            </div>
          ) : (
            <div className="alert alert-error" role="alert">
              {preview.error ?? 'That cron expression is not valid.'}
            </div>
          )
        ) : null}
      </section>

      <section className="panel">
        <h2>Existing schedules</h2>
        {schedules.loading && !schedules.data ? (
          <InlineLoader label="Loading schedules…" />
        ) : schedules.error ? (
          <ErrorState message={schedules.error} onRetry={schedules.reload} />
        ) : !schedules.data || schedules.data.items.length === 0 ? (
          <EmptyState
            title="No schedules yet"
            description="Add one above to run a published workflow automatically."
          />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Schedule</th>
                <th scope="col">Workflow</th>
                <th scope="col">Cadence</th>
                <th scope="col">Next run</th>
                <th scope="col">Last run</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {schedules.data.items.map((schedule) => (
                <tr key={schedule.id}>
                  <td>
                    <strong>{schedule.name}</strong>
                    <p className="row-subtitle">
                      <span className={`pill ${schedule.enabled ? 'pill-current' : ''}`}>
                        {schedule.enabled ? 'enabled' : 'paused'}
                      </span>
                      <span className="muted">
                        {' '}
                        v{schedule.effective_version} · {schedule.overlap_policy}
                      </span>
                    </p>
                  </td>
                  <td>
                    <Link to={`/workflows/${schedule.workflow_id}`}>{schedule.workflow_name || schedule.workflow_id}</Link>
                  </td>
                  <td>
                    <code>{schedule.cron_expression}</code>
                    <p className="row-subtitle muted">
                      {schedule.cron_description || schedule.timezone} · {schedule.timezone}
                    </p>
                  </td>
                  <td>{schedule.enabled ? formatTimestamp(schedule.next_run_at) : 'paused'}</td>
                  <td>
                    {schedule.last_status ? (
                      <>
                        <StatusBadge status={schedule.last_status} />
                        {schedule.last_run_id ? (
                          <Link className="muted" to={`/runs/${schedule.last_run_id}`}>
                            {' '}
                            {formatRelative(schedule.last_run_at)}
                          </Link>
                        ) : null}
                      </>
                    ) : (
                      <span className="muted">never</span>
                    )}
                  </td>
                  <td>
                    <div className="row-actions">
                      <button
                        type="button"
                        className="button button-ghost"
                        onClick={() => void toggleSchedule(schedule)}
                        disabled={busy === schedule.id}
                      >
                        {schedule.enabled ? 'Pause' : 'Resume'}
                      </button>
                      <button
                        type="button"
                        className="button button-ghost"
                        onClick={() => void runNow(schedule)}
                        disabled={busy === schedule.id}
                      >
                        Run now
                      </button>
                      <button
                        type="button"
                        className="button button-ghost"
                        onClick={() => {
                          const opening = backfillFor !== schedule.id
                          setBackfillFor(opening ? schedule.id : null)
                          if (opening) void loadBackfills(schedule.id)
                        }}
                      >
                        Backfill
                      </button>
                      <button
                        type="button"
                        className="button button-ghost button-danger"
                        onClick={() => void removeSchedule(schedule)}
                        disabled={busy === schedule.id}
                      >
                        Delete
                      </button>
                    </div>
                    {backfillFor === schedule.id ? (
                      <div className="backfill-panel">
                        <strong>Replay scheduled occurrences</strong>
                        <div className="row-actions">
                          <label className="field"><span>Start date (UTC)</span><input className="input" type="date" value={backfillStart} onChange={(event) => setBackfillStart(event.target.value)} /></label>
                          <label className="field"><span>End date (UTC, inclusive)</span><input className="input" type="date" value={backfillEnd} onChange={(event) => setBackfillEnd(event.target.value)} /></label>
                          <label className="field"><span>Concurrency</span><input className="input" type="number" min="1" max="64" value={backfillConcurrency} onChange={(event) => setBackfillConcurrency(event.target.value)} /></label>
                          <button type="button" className="button button-primary" onClick={() => void createBackfill(schedule)} disabled={busy === `backfill:${schedule.id}`}>{busy === `backfill:${schedule.id}` ? 'Starting…' : 'Start backfill'}</button>
                        </div>
                        <button type="button" className="button button-ghost" onClick={() => void loadBackfills(schedule.id)}>Refresh jobs</button>
                        {(backfills[schedule.id] ?? []).length ? <ul className="backfill-list">{backfills[schedule.id].map((job) => <li key={job.id}><span className={`pill ${job.status === 'completed' ? 'pill-current' : ''}`}>{job.status}</span> {formatTimestamp(job.start)} – {formatTimestamp(job.end)} · max {job.concurrency_limit} active</li>)}</ul> : <p className="muted">No backfill jobs yet.</p>}
                      </div>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}

export default SchedulesPage
