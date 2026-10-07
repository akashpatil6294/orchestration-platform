import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { ApiError, api } from '../lib/api'
import RunTimeline from '../components/RunTimeline'
import { useRunEventStream } from '../hooks/useRunEventStream'
import type { RunDetail, RunEvent, StepRun } from '../lib/types'
import { usePolling, useResource } from '../lib/useResource'
import { useConfirm } from '../components/ConfirmDialogProvider'
import { WorkerCapacityBanner } from '../components/WorkerCapacityBanner'
import { useToast } from '../components/ToastProvider'
import {
  EmptyState,
  ErrorState,
  InlineLoader,
  ProgressBar,
  StatusBadge,
  formatDuration,
  formatRelative,
  formatTimestamp,
} from '../components/StatusView'

interface AiUsageModel {
  model: string
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cost_usd?: number
  steps: number
}

interface RunStats {
  run_id: string
  status: string
  step_counts: Record<string, number>
  total_attempts: number
  retried_steps: number
  ai_usage: { models: AiUsageModel[]; steps: number } | null
}

/** Statuses that still change on their own, so polling stays on for them. */
const LIVE_STATUSES = new Set(['queued', 'running', 'retrying', 'cancelling', 'paused', 'waiting_approval', 'waiting_children', 'waiting_subworkflow'])

export interface StepAttemptView {
  id: string
  attempt: number
  worker_id: string | null
  status: string
  started_at: string | null
  finished_at: string | null
  duration_seconds: number | null
  output: unknown
  error: Record<string, unknown> | null
  error_message: string | null
  error_class: 'retryable' | 'permanent' | null
  log_lines: number
}

function StepCard({
  runId,
  step,
  onApproval,
  onRerunFromStep,
}: {
  runId: string
  step: StepRun
  onApproval: (stepKey: string, decision: 'approve' | 'reject', comment: string) => void
  onRerunFromStep: (stepKey: string) => void
}) {
  const [logs, setLogs] = useState<string[] | null>(null)
  const [logError, setLogError] = useState<string | null>(null)
  const [showOutput, setShowOutput] = useState(false)
  const [approvalComment, setApprovalComment] = useState('')
  const [attempts, setAttempts] = useState<StepAttemptView[] | null>(null)
  const [attemptError, setAttemptError] = useState<string | null>(null)

  async function loadLogs() {
    setLogError(null)
    try {
      const response = await api.get<{ lines: string[] }>(`/api/v1/runs/${runId}/steps/${step.id}/logs`)
      setLogs(response.lines ?? [])
    } catch (cause) {
      setLogError(cause instanceof ApiError ? cause.message : 'Logs could not be loaded.')
    }
  }

  async function loadAttempts() {
    setAttemptError(null)
    try {
      if (attempts !== null) {
        setAttempts(null)
        return
      }
      const response = await api.get<{ items: StepAttemptView[] }>(`/api/v1/runs/${runId}/steps/${step.id}/attempts`)
      setAttempts(response.items)
    } catch (cause) {
      setAttemptError(cause instanceof ApiError ? cause.message : 'Attempts could not be loaded.')
    }
  }

  return (
    <li className="step-card">
      <div className="step-head">
        <div>
          <strong>{step.name || step.key}</strong>
          <code className="step-type">{step.type}</code>
        </div>
        <StatusBadge status={step.status} />
      </div>

      <dl className="step-meta">
        <div>
          <dt>Attempts</dt>
          <dd>
            {step.attempts}
            {step.retry_limit > 0 ? ` / ${step.retry_limit + 1}` : ''}
          </dd>
        </div>
        <div>
          <dt>Duration</dt>
          <dd>{formatDuration(step.duration_seconds)}</dd>
        </div>
        <div>
          <dt>Timeout</dt>
          <dd>{step.timeout_seconds}s</dd>
        </div>
        <div>
          <dt>Requirement</dt>
          <dd>{step.required ? 'Required' : 'Optional'}</dd>
        </div>
        <div>
          <dt>Worker</dt>
          <dd>{step.worker_id ?? '—'}</dd>
        </div>
        <div>
          <dt>Depends on</dt>
          <dd>{step.depends_on.length ? step.depends_on.join(', ') : '—'}</dd>
        </div>
        <div>
          <dt>Next attempt</dt>
          <dd>{step.retry_at ? formatTimestamp(step.retry_at) : '—'}</dd>
        </div>
        {step.wait_reason ? (
          <div>
            <dt>Waiting</dt>
            <dd title={waitReasonDetail(step.wait_reason)}>{waitReasonLabel(step.wait_reason)}</dd>
          </div>
        ) : null}
      </dl>

      {step.error ? (
        <div className="alert alert-error" role="alert">
          <strong>{String(step.error.code ?? 'failed')}</strong>
          <span>{String(step.error.message ?? 'This step failed.')}</span>
        </div>
      ) : null}

      {step.status === 'waiting_approval' ? (
        <div className="approval-actions">
          <label>
            Approval comment
            <textarea value={approvalComment} onChange={(event) => setApprovalComment(event.target.value)} maxLength={1000} />
          </label>
          <button type="button" className="button button-secondary" onClick={() => onApproval(step.key, 'reject', approvalComment)}>
            Reject
          </button>
          <button type="button" className="button button-primary" onClick={() => onApproval(step.key, 'approve', approvalComment)}>
            Approve
          </button>
        </div>
      ) : null}

      <div className="step-actions">
        <button type="button" className="button button-ghost" onClick={() => setShowOutput((value) => !value)}>
          {showOutput ? 'Hide output' : 'Show output'}
        </button>
        <button type="button" className="button button-ghost" onClick={() => void loadLogs()}>
          Load logs {step.log_lines > 0 ? `(${step.log_lines})` : ''}
        </button>
        <button type="button" className="button button-ghost" aria-expanded={attempts !== null} onClick={() => void loadAttempts()}>
          {attempts !== null ? 'Hide attempts' : `View attempts (${step.attempts})`}
        </button>
        {step.status === 'failed' ? (
          <button type="button" className="button button-ghost" onClick={() => onRerunFromStep(step.key)}>
            Re-run from here
          </button>
        ) : null}
      </div>

      {attempts !== null ? (
        attempts.length === 0 ? (
          <p className="muted">No attempts recorded yet.</p>
        ) : (
          <table className="table" aria-label={`Attempt history for ${step.key}`}>
            <thead>
              <tr>
                <th scope="col">#</th>
                <th scope="col">Status</th>
                <th scope="col">Worker</th>
                <th scope="col">Error class</th>
                <th scope="col">Duration</th>
                <th scope="col">Started</th>
                <th scope="col">Finished</th>
              </tr>
            </thead>
            <tbody>
              {attempts.map((attempt) => (
                <tr key={attempt.id}>
                  <td>{attempt.attempt}</td>
                  <td>
                    <StatusBadge status={attempt.status} />
                  </td>
                  <td>{attempt.worker_id ?? '—'}</td>
                  <td>
                    {attempt.error_class ? (
                      <span className={`error-class error-${attempt.error_class}`} title={attempt.error_message ?? undefined}>
                        {attempt.error_class}
                      </span>
                    ) : '—'}
                  </td>
                  <td>{formatDuration(attempt.duration_seconds)}</td>
                  <td>{formatTimestamp(attempt.started_at)}</td>
                  <td>{formatTimestamp(attempt.finished_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      ) : null}
      {attemptError ? <p className="muted">{attemptError}</p> : null}

      {showOutput ? (
        <pre className="code-block">{step.output === null || step.output === undefined ? 'No output yet.' : JSON.stringify(step.output, null, 2)}</pre>
      ) : null}

      {logs ? (
        logs.length ? (
          <pre className="code-block code-block-logs">{logs.join('\n')}</pre>
        ) : (
          <p className="muted">This step has not written any logs.</p>
        )
      ) : null}
      {logError ? <p className="muted">{logError}</p> : null}
    </li>
  )
}

const WAIT_REASON_LABELS: Record<string, string> = {
  waiting_for_dependencies: 'Waiting for dependencies',
  waiting_for_worker: 'Waiting for a worker',
  rate_limited: 'Rate limited',
  concurrency_limited: 'Concurrency limited',
  circuit_open: 'Circuit breaker open',
  approval_pending: 'Waiting for approval',
  paused: 'Run paused',
}

const WAIT_REASON_DETAILS: Record<string, string> = {
  waiting_for_dependencies: 'Upstream steps have not finished successfully yet.',
  waiting_for_worker: 'The step is ready but no worker has claimed it.',
  rate_limited: 'A task-type rate limit is holding this step back.',
  concurrency_limited: 'A concurrency budget (run, queue, owner or resource) is exhausted.',
  circuit_open: 'The circuit breaker for this task type is open after repeated failures.',
  approval_pending: 'A human must approve or reject this step before it continues.',
  paused: 'The run is paused; resume it to continue.',
}

function waitReasonLabel(reason: string): string {
  return WAIT_REASON_LABELS[reason] ?? reason
}

function waitReasonDetail(reason: string): string {
  return WAIT_REASON_DETAILS[reason] ?? ''
}

export function RunDetailPage() {
  const { runId = '' } = useParams()
  const navigate = useNavigate()
  const toast = useToast()
  const confirmAction = useConfirm()
  const [actionError, setActionError] = useState<string | null>(null)
  const [acting, setActing] = useState<'cancel' | 'retry' | 'pause' | 'resume' | string | null>(null)
  const [showReplay, setShowReplay] = useState(false)
  const [replayInput, setReplayInput] = useState('')
  const [showEvents, setShowEvents] = useState(false)

  const run = useResource<RunDetail>((signal) => api.get<RunDetail>(`/api/v1/runs/${runId}`, signal), [runId])
  const events = useResource<{ items: RunEvent[]; latest_seq: number }>(
    (signal) => api.get<{ items: RunEvent[]; latest_seq: number }>(`/api/v1/runs/${runId}/events?limit=50`, signal),
    [runId],
  )
  const stats = useResource<RunStats>((signal) => api.get<RunStats>(`/api/v1/runs/${runId}/stats`, signal), [runId])

  const isLive = run.data ? LIVE_STATUSES.has(run.data.status) : false
  usePolling(() => run.reload(), 3000, isLive)
  usePolling(() => events.reload(), 5000, isLive)
  usePolling(() => stats.reload(), 5000, isLive)
  const liveStream = useRunEventStream(runId, isLive && showEvents, events.data?.latest_seq ?? 0)
  const mergedEvents = [...(events.data?.items ?? [])]
  for (const streamed of liveStream.events) {
    if (!mergedEvents.some((e) => e.seq === streamed.seq)) {
      mergedEvents.push({ ...streamed, actor: null, payload: {}, created_at: streamed.created_at ?? '' })
    }
  }
  mergedEvents.sort((a, b) => a.seq - b.seq)

  async function cancelRun() {
    const ok = await confirmAction.confirm({
      title: 'Cancel run',
      message: 'Request cancellation of this run? Running steps are asked to stop.',
      confirmLabel: 'Cancel run',
      danger: true,
    })
    if (!ok) return
    setActing('cancel')
    setActionError(null)
    try {
      await api.post(`/api/v1/runs/${runId}/cancel`)
      toast.success('Cancellation requested')
      run.reload()
      events.reload()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The run could not be cancelled.')
    } finally {
      setActing(null)
    }
  }

  async function retryRun() {
    const ok = await confirmAction.confirm({
      title: 'Retry failed steps',
      message: 'Retry every failed step in this run? Downstream steps are re-run as well.',
      confirmLabel: 'Retry',
    })
    if (!ok) return
    setActing('retry')
    setActionError(null)
    try {
      const result = await api.post<{ retried_steps: string[] }>(`/api/v1/runs/${runId}/retry`, {
        steps: [],
        reset_downstream: true,
      })
      if (result.retried_steps.length === 0) {
        setActionError('There is no failed work to retry in this run.')
      } else {
        toast.success('Retry scheduled', `${result.retried_steps.length} step${result.retried_steps.length === 1 ? '' : 's'} queued again.`)
      }
      run.reload()
      events.reload()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The run could not be retried.')
    } finally {
      setActing(null)
    }
  }

  async function rerunFromStep(stepKey: string) {
    const ok = await confirmAction.confirm({
      title: 'Re-run from step',
      message: `Re-run "${stepKey}" and every step after it? Results of later steps are discarded.`,
      confirmLabel: 'Re-run',
    })
    if (!ok) return
    setActing(`rerun:${stepKey}`)
    setActionError(null)
    try {
      await api.post(`/api/v1/runs/${runId}/rerun-from-step`, { steps: [stepKey], reset_downstream: true })
      toast.success('Re-run scheduled', `Starting again from "${stepKey}".`)
      run.reload()
      events.reload()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The step could not be re-run.')
    } finally {
      setActing(null)
    }
  }

  async function deleteRun() {
    const ok = await confirmAction.confirm({
      title: 'Delete run',
      message: 'Permanently delete this run, its steps and events? This cannot be undone.',
      confirmLabel: 'Delete run',
      danger: true,
    })
    if (!ok) return
    setActing('delete')
    setActionError(null)
    try {
      await api.remove(`/api/v1/runs/${runId}`)
      toast.success('Run deleted')
      navigate(`/workflows/${run.data?.workflow_id ?? ''}`)
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The run could not be deleted. Cancel it first if it is still active.')
    } finally {
      setActing(null)
    }
  }

  async function setRunPaused(paused: boolean) {
    setActing(paused ? 'pause' : 'resume')
    setActionError(null)
    try {
      await api.post(`/api/v1/runs/${runId}/${paused ? 'pause' : 'resume'}`)
      toast.success(paused ? 'Run paused' : 'Run resumed')
      run.reload()
      events.reload()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : `The run could not be ${paused ? 'paused' : 'resumed'}.`)
    } finally {
      setActing(null)
    }
  }

  async function decideApproval(stepKey: string, decision: 'approve' | 'reject', comment: string) {
    if (decision === 'reject') {
      const ok = await confirmAction.confirm({
        title: 'Reject approval',
        message: `Reject step "${stepKey}"? The run follows its rejection policy (usually skipping downstream steps).`,
        confirmLabel: 'Reject',
        danger: true,
      })
      if (!ok) return
    }
    setActing(stepKey)
    setActionError(null)
    try {
      await api.post(`/api/v1/runs/${runId}/steps/${encodeURIComponent(stepKey)}/approval`, { decision, comment })
      toast.success(decision === 'approve' ? 'Step approved' : 'Step rejected')
      run.reload()
      events.reload()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The approval decision could not be saved.')
    } finally {
      setActing(null)
    }
  }

  if (run.loading && !run.data) return <InlineLoader label="Loading run…" />
  if (run.error) return <ErrorState message={run.error} onRetry={run.reload} />
  if (!run.data) return null

  const detail = run.data
  const retryable = detail.retryable_steps.length > 0

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <nav className="breadcrumb" aria-label="Breadcrumb">
            <Link to="/dashboard">Dashboard</Link>
            <span aria-hidden="true">/</span>
            <Link to={`/workflows/${detail.workflow_id}`}>{detail.workflow_name || 'Workflow'}</Link>
            <span aria-hidden="true">/</span>
            <span>Run</span>
          </nav>
          <h1>
            Run <code>{detail.id.slice(0, 12)}</code>
          </h1>
          <p className="page-subtitle">
            Version v{detail.version} · {detail.trigger} · started {formatRelative(detail.started_at ?? detail.created_at)}
            {isLive ? ' · updating live' : ''}
          </p>
        </div>
        <WorkerCapacityBanner
          scope={detail.steps
            .filter((step) => step.wait_reason === 'waiting_for_worker')
            .map((step) => ({ queue: step.queue, taskType: step.type }))}
        />
        <div className="page-actions">
          {detail.status === 'paused' ? (
            <button type="button" className="button button-secondary" onClick={() => void setRunPaused(false)} disabled={acting !== null}>
              {acting === 'resume' ? 'Resuming…' : 'Resume run'}
            </button>
          ) : (
            <button type="button" className="button button-secondary" onClick={() => void setRunPaused(true)} disabled={acting !== null || !isLive}>
              {acting === 'pause' ? 'Pausing…' : 'Pause run'}
            </button>
          )}
          <button
            type="button"
            className="button button-secondary"
            onClick={() => void cancelRun()}
            disabled={acting !== null || !isLive}
          >
            {acting === 'cancel' ? 'Cancelling…' : 'Cancel run'}
          </button>
          <button
            type="button"
            className="button button-primary"
            onClick={() => void retryRun()}
            disabled={acting !== null || !retryable}
            title={retryable ? undefined : 'No failed steps are eligible for a retry'}
          >
            {acting === 'retry' ? 'Retrying…' : 'Retry failed steps'}
          </button>
          <button
            type="button"
            className="button button-secondary"
            onClick={() => setShowReplay(true)}
            disabled={acting !== null}
            title="Create a new run from this one with edited input"
          >
            Replay with edited input
          </button>
          {!isLive && detail.status !== 'queued' ? (
            <button
              type="button"
              className="button button-danger"
              onClick={() => void deleteRun()}
              disabled={acting !== null}
              title="Delete this finished run"
            >
              {acting === 'delete' ? 'Deleting…' : 'Delete run'}
            </button>
          ) : null}
        </div>
      </header>

      {actionError ? (
        <div className="alert alert-error" role="alert">
          {actionError}
        </div>
      ) : null}

      <section className="panel">
        <div className="run-summary">
          <div>
            <span className="stat-label">Status</span>
            <StatusBadge status={detail.status} />
          </div>
          <div>
            <span className="stat-label">Progress</span>
            <ProgressBar value={detail.progress} />
            <span className="muted">
              {detail.completed_steps}/{detail.total_steps} steps
            </span>
          </div>
          <div>
            <span className="stat-label">Duration</span>
            <strong>{formatDuration(detail.duration_seconds)}</strong>
          </div>
          <div>
            <span className="stat-label">Parallelism</span>
            <strong>{detail.max_parallel}</strong>
          </div>
        </div>

        {detail.error ? (
          <div className="alert alert-error" role="alert">
            <strong>{String(detail.error.code ?? 'run_failed')}</strong>
            <span>{String(detail.error.message ?? 'This run failed.')}</span>
          </div>
        ) : null}
        {detail.cancel_requested && isLive ? (
          <p className="muted">Cancellation requested; workers are finishing their current steps.</p>
        ) : null}
      </section>

      {stats.data?.ai_usage ? (
        <section className="panel" aria-label="AI usage">
          <h2>AI usage</h2>
          <p className="muted">
            {stats.data.ai_usage.steps} step{stats.data.ai_usage.steps === 1 ? '' : 's'} reported token usage.
          </p>
          <ul className="step-list">
            {stats.data.ai_usage.models.map((model) => (
              <li key={model.model} className="step-card">
                <div className="run-summary">
                  <div>
                    <span className="stat-label">Model</span>
                    <strong>{model.model}</strong>
                  </div>
                  <div>
                    <span className="stat-label">Tokens</span>
                    <strong>{model.total_tokens.toLocaleString()}</strong>
                    <span className="muted">
                      {model.prompt_tokens.toLocaleString()} in / {model.completion_tokens.toLocaleString()} out
                    </span>
                  </div>
                  <div>
                    <span className="stat-label">Steps</span>
                    <strong>{model.steps}</strong>
                  </div>
                  {model.cost_usd !== undefined ? (
                    <div>
                      <span className="stat-label">Cost</span>
                      <strong>${model.cost_usd.toFixed(6)}</strong>
                    </div>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="panel">
        <h2>Steps</h2>
        <ul className="step-list">
          {detail.steps.map((step) => (
            <StepCard
              key={step.id}
              runId={detail.id}
              step={step}
              onApproval={(stepKey, decision, comment) => void decideApproval(stepKey, decision, comment)}
              onRerunFromStep={(stepKey) => void rerunFromStep(stepKey)}
            />
          ))}
        </ul>
      </section>

      <div className="two-column">
        <section className="panel">
          <h2>Run input</h2>
          <pre className="code-block">{JSON.stringify(detail.input ?? {}, null, 2)}</pre>
          <h3 className="panel-subheading">Run output</h3>
          <pre className="code-block">
            {detail.output === null || detail.output === undefined
              ? 'No output recorded yet.'
              : JSON.stringify(detail.output, null, 2)}
          </pre>
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2>Step timeline</h2>
            <Link className="button button-ghost button-sm" to={`/runs/compare?a=${runId}`}>
              Compare
            </Link>
          </div>
          {run.data ? <RunTimeline steps={run.data.steps} /> : <InlineLoader label="Loading timeline…" />}
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2>
              Event timeline{' '}
              {isLive && showEvents ? (
                <span className={`live-badge${liveStream.connected ? ' live-on' : ''}`} title="Live event stream">
                  ● {liveStream.connected ? 'live' : 'connecting…'}
                </span>
              ) : null}
            </h2>
            <button type="button" className="button button-ghost" onClick={() => setShowEvents((value) => !value)}>
              {showEvents ? 'Hide' : 'Show'}
            </button>
          </div>
          {!showEvents ? (
            <p className="muted">Event history is hidden. Showing it is useful when troubleshooting a failure.</p>
          ) : events.loading && !events.data ? (
            <InlineLoader label="Loading events…" />
          ) : mergedEvents.length === 0 ? (
            <EmptyState title="No events" description="This run has not recorded any lifecycle events." />
          ) : (
            <ol className="event-list">
              {mergedEvents
                .slice()
                .reverse()
                .map((event) => (
                  <li key={event.id} className={`activity activity-${event.level}`}>
                    <span className="activity-type">{event.type.replace(/[._]/g, ' ')}</span>
                    <span className="activity-message">
                      {event.message}
                      {event.step_key ? ` (${event.step_key})` : ''}
                    </span>
                    <span className="activity-time">{formatRelative(event.created_at)}</span>
                  </li>
                ))}
            </ol>
          )}
        </section>
      </div>

      {showReplay && (
        <ReplayModal
          initialInput={detail.input ?? {}}
          onClose={() => setShowReplay(false)}
          onReplay={async (input) => {
            setActing('replay')
            setActionError(null)
            try {
              const result = await api.post<{ id: string }>(`/api/v1/runs/${runId}/replay`, { input })
              setShowReplay(false)
              navigate(`/runs/${result.id}`)
            } catch (cause) {
              setActionError(cause instanceof ApiError ? cause.message : 'The run could not be replayed.')
            } finally {
              setActing(null)
            }
          }}
        />
      )}
    </div>
  )
}

function ReplayModal(props: {
  initialInput: Record<string, unknown>
  onClose: () => void
  onReplay: (input: Record<string, unknown>) => Promise<void>
}) {
  const [text, setText] = useState(() => JSON.stringify(props.initialInput, null, 2))
  const [error, setError] = useState<string | null>(null)

  const submit = () => {
    try {
      const parsed = JSON.parse(text) as Record<string, unknown>
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        setError('Input must be a JSON object.')
        return
      }
      setError(null)
      void props.onReplay(parsed)
    } catch {
      setError('Input must be valid JSON.')
    }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Replay run">
      <div className="modal modal-wide">
        <h2>Replay with edited input</h2>
        <p className="muted">Creates a new run linked to this one. The original run is untouched.</p>
        <label>
          Workflow input (JSON)
          <textarea value={text} onChange={(e) => setText(e.target.value)} rows={10} spellCheck={false} />
        </label>
        {error && <p className="code-view-error" role="alert">{error}</p>}
        <div className="modal-actions">
          <button onClick={props.onClose} className="btn-ghost">Cancel</button>
          <button onClick={submit}>Replay as new run</button>
        </div>
      </div>
    </div>
  )
}

export default RunDetailPage
