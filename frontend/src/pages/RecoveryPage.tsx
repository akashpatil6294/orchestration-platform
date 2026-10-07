/**
 * Recovery page: crashed runs, DLQ depth, requeue actions, failed webhooks.
 * (Stage H, H5)
 */
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../lib/api'

interface RecoverySummary {
  crashed_runs: number
  dlq_depth: number
  queued_runs: number
}

interface CrashedRun {
  run_id: string
  workflow_id: string
  workflow_name: string
  status: string
  expired_steps: number
  updated_at: string
}

interface FailedChannel {
  id: string
  name: string
  channel_type: string
  workflow_id: string | null
  events: string[]
}

export default function RecoveryPage() {
  const [summary, setSummary] = useState<RecoverySummary | null>(null)
  const [crashed, setCrashed] = useState<CrashedRun[]>([])
  const [channels, setChannels] = useState<FailedChannel[]>([])
  const [error, setError] = useState<string | null>(null)
  const [acting, setActing] = useState<string | null>(null)

  const load = async () => {
    try {
      const [s, c, w] = await Promise.all([
        api.get<RecoverySummary>('/api/v1/ops/recovery/summary'),
        api.get<{ items: CrashedRun[] }>('/api/v1/ops/recovery/crashed'),
        api.get<{ items: FailedChannel[] }>('/api/v1/ops/recovery/webhooks/failed'),
      ])
      setSummary(s)
      setCrashed(c.items)
      setChannels(w.items)
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to load recovery data')
    }
  }

  useEffect(() => {
    void load()
  }, [])

  const requeueRun = async (runId: string) => {
    setActing(runId)
    try {
      await api.post(`/api/v1/runs/${runId}/retry`, { steps: [], reset_downstream: true })
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Requeue failed')
    } finally {
      setActing(null)
    }
  }

  const retryChannel = async (id: string) => {
    setActing(id)
    try {
      await api.post(`/api/v1/ops/recovery/webhooks/${id}/retry`)
      await load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Retry failed')
    } finally {
      setActing(null)
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <h1>Recovery</h1>
        <button onClick={() => void load()}>Refresh</button>
      </header>

      {error && <p className="alert alert-error" role="alert">{error}</p>}

      {summary && (
        <section className="stat-row">
          <div className="stat"><span className="stat-value">{summary.crashed_runs}</span><span className="stat-label">Crashed runs</span></div>
          <div className="stat"><span className="stat-value">{summary.dlq_depth}</span><span className="stat-label">DLQ depth</span></div>
          <div className="stat"><span className="stat-value">{summary.queued_runs}</span><span className="stat-label">Queued runs</span></div>
        </section>
      )}

      <section className="panel">
        <h2>Crashed runs</h2>
        <p className="muted">Runs with lease-expired steps — a worker died mid-run. Another worker picks these up automatically; requeue to force it.</p>
        {crashed.length === 0 ? (
          <p className="muted">No crashed runs.</p>
        ) : (
          <table className="table">
            <thead><tr><th>Run</th><th>Workflow</th><th>Status</th><th>Expired steps</th><th></th></tr></thead>
            <tbody>
              {crashed.map((r) => (
                <tr key={r.run_id}>
                  <td><Link to={`/runs/${r.run_id}`}>{r.run_id.slice(0, 8)}</Link></td>
                  <td>{r.workflow_name}</td>
                  <td>{r.status}</td>
                  <td>{r.expired_steps}</td>
                  <td>
                    <button onClick={() => void requeueRun(r.run_id)} disabled={acting === r.run_id} className="button button-small">
                      {acting === r.run_id ? 'Requeuing…' : 'Requeue'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="panel">
        <h2>Dead-letter queue</h2>
        <p className="muted">{summary?.dlq_depth ?? 0} permanently failed step(s). <Link to="/ops/dlq">Open the DLQ</Link> to redrive individual steps.</p>
      </section>

      <section className="panel">
        <h2>Failed webhooks</h2>
        <p className="muted">Disabled notification channels, usually after repeated delivery failures.</p>
        {channels.length === 0 ? (
          <p className="muted">No failed webhooks.</p>
        ) : (
          <table className="table">
            <thead><tr><th>Channel</th><th>Type</th><th>Events</th><th></th></tr></thead>
            <tbody>
              {channels.map((c) => (
                <tr key={c.id}>
                  <td>{c.name}</td>
                  <td>{c.channel_type}</td>
                  <td>{c.events.join(', ')}</td>
                  <td>
                    <button onClick={() => void retryChannel(c.id)} disabled={acting === c.id} className="button button-small">
                      {acting === c.id ? 'Retrying…' : 'Re-enable'}
                    </button>
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
