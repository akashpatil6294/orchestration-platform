/**
 * Fleet and queue operations.
 *
 * Combines `/api/v1/ops/overview` (queue depth, scheduler health, dispatch
 * backend) with the worker fleet from `/api/v1/workers`: activate, deactivate
 * and graceful shutdown, per-worker in-flight tasks, stale highlighting and
 * the three maintenance actions. The overview refreshes every 15 seconds while
 * the tab is visible.
 */
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../lib/api'
import type { OpsOverview, WorkerTaskItem, WorkerView } from '../lib/types'
import { useLiveResource } from '../lib/useLiveResource'
import { useConfirm } from '../components/ConfirmDialogProvider'
import { useToast } from '../components/ToastProvider'
import { ActionButton } from '../components/ActionButton'
import { EmptyState, ErrorState, InlineLoader, StatusBadge, formatRelative } from '../components/StatusView'

export function WorkersPage() {
  const toast = useToast()
  const confirmAction = useConfirm()
  const [workerTasks, setWorkerTasks] = useState<Record<string, WorkerTaskItem[]>>({})
  const [expandedWorker, setExpandedWorker] = useState<string | null>(null)

  const overview = useLiveResource<OpsOverview>((signal) => api.get<OpsOverview>('/api/v1/ops/overview', signal), [], 15000, true)

  const workers: WorkerView[] = overview.data?.workers.items ?? []

  const refreshAll = useCallback(() => {
    overview.refresh()
  }, [overview])

  // Re-fetch the expanded worker's in-flight tasks whenever the overview updates.
  useEffect(() => {
    if (!expandedWorker) return
    const controller = new AbortController()
    api
      .get<{ items: WorkerTaskItem[] }>(`/api/v1/workers/${expandedWorker}/tasks`, controller.signal)
      .then((response) => setWorkerTasks((current) => ({ ...current, [expandedWorker]: response.items })))
      .catch(() => undefined)
    return () => controller.abort()
  }, [expandedWorker, overview.lastUpdatedAt])

  const setWorkerActive = async (worker: WorkerView, active: boolean) => {
    await api.post(`/api/v1/workers/${worker.worker_id}/${active ? 'activate' : 'deactivate'}`)
    toast.success(active ? 'Worker activated' : 'Worker deactivated', `${worker.name || worker.worker_id} is now ${active ? 'accepting' : 'declining'} new work.`)
    refreshAll()
  }

  const shutdown = async (worker: WorkerView) => {
    const ok = await confirmAction.confirm({
      title: 'Graceful shutdown',
      message: `Deactivate ${worker.name || worker.worker_id} and release its ${worker.running_steps} running step${worker.running_steps === 1 ? '' : 's'} back to the queue?`,
      confirmLabel: 'Shut down',
      danger: true,
    })
    if (!ok) return
    const result = await api.post<{ released_tasks: number; message: string }>(`/api/v1/workers/${worker.worker_id}/shutdown`)
    toast.success('Shutdown complete', `${result.released_tasks} task${result.released_tasks === 1 ? '' : 's'} released. ${result.message}`.trim())
    refreshAll()
  }

  const maintenance = async (kind: 'recover-leases' | 'prune-outbox' | 'scheduler-tick') => {
    const result = await api.post<Record<string, number>>(`/api/v1/ops/maintenance/${kind}`)
    const label = kind === 'recover-leases' ? 'Leases recovered' : kind === 'prune-outbox' ? 'Outbox pruned' : 'Scheduler tick done'
    const detail =
      kind === 'recover-leases'
        ? `${result.recovered ?? 0} lease${(result.recovered ?? 0) === 1 ? '' : 's'} reclaimed.`
        : kind === 'prune-outbox'
          ? `${result.removed ?? 0} message${(result.removed ?? 0) === 1 ? '' : 's'} removed.`
          : `${result.runs_started ?? 0} run${(result.runs_started ?? 0) === 1 ? '' : 's'} started from due schedules.`
    toast.success(label, detail)
    refreshAll()
  }

  if (overview.initial) return <InlineLoader label="Loading fleet overview…" />
  if (overview.error && !overview.data) return <ErrorState message={overview.error ?? ''} onRetry={overview.refresh} />
  if (!overview.data) return null

  const data = overview.data
  const dispatch = data.dispatch
  const scheduler = data.scheduler
  const updatedLabel = overview.hidden
    ? 'Paused (tab hidden)'
    : overview.secondsAgo === null
      ? 'Updated just now'
      : `Updated ${overview.secondsAgo}s ago`

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>Workers &amp; queues</h1>
          <p className="page-subtitle">Fleet health, queue depth and operator maintenance.</p>
        </div>
        <div className="live-controls">
          <span className="updated-ago" aria-live="polite">
            {updatedLabel}
          </span>
          <button type="button" className="button button-secondary" onClick={overview.refresh} disabled={overview.loading}>
            {overview.loading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </header>

      <section className="panel" aria-label="Platform status">
        <h2>Platform status</h2>
        <dl className="kv-grid">
          <div>
            <dt>Environment</dt>
            <dd>
              {data.environment} · v{data.version}
            </dd>
          </div>
          <div>
            <dt>Database</dt>
            <dd>{data.database}</dd>
          </div>
          <div>
            <dt>Dispatch backend</dt>
            <dd>
              {dispatch.backend}
              {dispatch.redis_configured ? (dispatch.relay_running ? ' · relay running' : ' · relay stopped') : ''}
            </dd>
          </div>
          <div>
            <dt>Outbox backlog</dt>
            <dd>{dispatch.outbox_backlog}</dd>
          </div>
          <div>
            <dt>Scheduler</dt>
            <dd>
              {scheduler.enabled ? (scheduler.loop_running ? 'Running' : 'Stopped') : 'Disabled'} · {scheduler.enabled_schedules} enabled ·{' '}
              {scheduler.due_schedules} due
            </dd>
          </div>
          {scheduler.last_tick_at ? (
            <div>
              <dt>Last scheduler tick</dt>
              <dd>{formatRelative(scheduler.last_tick_at)}</dd>
            </div>
          ) : null}
          {scheduler.last_error ? (
            <div>
              <dt>Scheduler error</dt>
              <dd className="stale-flag">{scheduler.last_error}</dd>
            </div>
          ) : null}
        </dl>
        <dl className="kv-grid" style={{ marginTop: '0.8rem' }}>
          {Object.entries(data.steps.by_status)
            .sort()
            .map(([status, count]) => (
              <div key={status}>
                <dt>{status} steps</dt>
                <dd>{count}</dd>
              </div>
            ))}
        </dl>
      </section>

      <section className="panel" aria-label="Maintenance">
        <h2>Maintenance</h2>
        <p className="muted">Operator actions run immediately against the current database.</p>
        <span className="row-actions">
          <ActionButton
            label="Recover expired leases"
            pendingLabel="Recovering…"
            errorTitle="Lease recovery failed"
            onAction={() => maintenance('recover-leases')}
          />
          <ActionButton label="Prune outbox" pendingLabel="Pruning…" errorTitle="Outbox prune failed" onAction={() => maintenance('prune-outbox')} />
          <ActionButton label="Run scheduler tick" pendingLabel="Ticking…" errorTitle="Scheduler tick failed" onAction={() => maintenance('scheduler-tick')} />
        </span>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2>
            Workers ({data.workers.total} total · {data.workers.active} active · {data.workers.stale} stale)
          </h2>
        </div>
        {workers.length === 0 ? (
          <EmptyState
            title="No workers registered"
            description="Create a worker token in Settings and start a worker to execute queued steps."
          />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Worker</th>
                <th scope="col">Status</th>
                <th scope="col">Load</th>
                <th scope="col">Task types</th>
                <th scope="col">Queues</th>
                <th scope="col">Last seen</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {workers.map((worker) => (
                <tr key={worker.worker_id} className={worker.stale ? 'row-stale' : undefined}>
                  <td>
                    <strong>{worker.name || worker.worker_id}</strong>
                    <p className="row-subtitle">{worker.worker_id}</p>
                  </td>
                  <td>
                    <StatusBadge status={worker.stale ? 'stale' : worker.active ? 'active' : 'idle'} />
                  </td>
                  <td>
                    {worker.running_steps}/{worker.max_concurrency} slots
                  </td>
                  <td>{worker.task_types.length ? worker.task_types.join(', ') : '—'}</td>
                  <td>{worker.queues.join(', ')}</td>
                  <td>{formatRelative(worker.last_seen_at)}</td>
                  <td>
                    <span className="row-actions">
                      {!worker.active ? (
                        <ActionButton label="Activate" errorTitle="Activation failed" onAction={() => setWorkerActive(worker, true)} />
                      ) : (
                        <ActionButton label="Deactivate" errorTitle="Deactivation failed" onAction={() => setWorkerActive(worker, false)} />
                      )}
                      <ActionButton label="Shutdown" variant="danger" errorTitle="Shutdown failed" onAction={() => shutdown(worker)} />
                      <button
                        type="button"
                        className="button button-ghost"
                        aria-expanded={expandedWorker === worker.worker_id}
                        onClick={() => setExpandedWorker((current) => (current === worker.worker_id ? null : worker.worker_id))}
                      >
                        {expandedWorker === worker.worker_id ? 'Hide tasks' : 'View tasks'}
                      </button>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {expandedWorker ? (
          <div className="backfill-panel" aria-label={`Tasks running on ${expandedWorker}`}>
            <h3>Running tasks on {expandedWorker}</h3>
            {(workerTasks[expandedWorker] ?? []).length === 0 ? (
              <p className="muted">This worker is not executing anything right now.</p>
            ) : (
              <ul className="activity-list">
                {(workerTasks[expandedWorker] ?? []).map((task) => (
                  <li key={task.step_run_id} className="activity">
                    <span className="activity-type">
                      <Link to={`/runs/${task.run_id}`}>{task.step_key}</Link>
                    </span>
                    <span className="activity-time">attempt {task.attempt}</span>
                    <span className="activity-message">
                      {task.task_type} · deadline {formatRelative(task.deadline_at)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : null}
      </section>
    </div>
  )
}

export default WorkersPage
