import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { useResource } from '../lib/useResource'

interface SessionInfo {
  user: { is_admin: boolean }
}

interface AuditEvent {
  id: string
  created_at: string
  actor_user_id: string | null
  actor_token_id: string | null
  action: string
  resource_type: string | null
  resource_id: string | null
  ip_address: string | null
}

export default function AuditPage() {
  const session = useResource<SessionInfo>((signal) => api.get<SessionInfo>('/api/v1/auth/session', signal), [])
  const [events, setEvents] = useState<AuditEvent[]>([])
  const [total, setTotal] = useState(0)
  const [actionFilter, setActionFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const isAdmin = session.data?.user?.is_admin ?? false

  async function load() {
    if (!isAdmin) return
    setLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams()
      if (actionFilter) params.set('action', actionFilter)
      const data = await api.get<{ items: AuditEvent[]; total: number }>(
        `/api/v1/audit/events?${params.toString()}`,
      )
      setEvents(data.items)
      setTotal(data.total)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not load audit events.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAdmin])

  if (!isAdmin) {
    return (
      <div className="page">
        <h1>Audit log</h1>
        <p className="muted">Administrator access is required to view the audit log.</p>
      </div>
    )
  }

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Audit log</h1>
          <p className="muted">
            Append-only record of security-relevant actions: who did what, when, and from where.
          </p>
        </div>
        <a className="button button-secondary" href="/api/v1/audit/events.csv" download>
          Export CSV
        </a>
      </div>

      <div className="filters">
        <input
          className="input"
          placeholder="Filter by action (e.g. team.create)"
          aria-label="Filter by action"
          value={actionFilter}
          onChange={(event) => setActionFilter(event.target.value)}
        />
        <button type="button" className="button button-secondary" onClick={load}>
          Apply
        </button>
      </div>

      {loading && <p className="muted">Loading…</p>}
      {error && <div className="alert alert-error">{error}</div>}

      {!loading && !error && (
        <>
          <p className="muted">{total} events</p>
          <table className="table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Action</th>
                <th>Actor</th>
                <th>Resource</th>
                <th>IP</th>
              </tr>
            </thead>
            <tbody>
              {events.map((event) => (
                <tr key={event.id}>
                  <td>{new Date(event.created_at).toLocaleString()}</td>
                  <td>
                    <code>{event.action}</code>
                  </td>
                  <td>
                    {event.actor_user_id ? (
                      <span title={event.actor_token_id ? `Token ${event.actor_token_id}` : 'Session'}>
                        {event.actor_user_id.slice(0, 8)}
                        {event.actor_token_id ? ' (token)' : ''}
                      </span>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td>
                    {event.resource_type ? `${event.resource_type}:${event.resource_id?.slice(0, 8)}` : '—'}
                  </td>
                  <td>{event.ip_address ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {events.length === 0 && <p className="muted">No events match.</p>}
        </>
      )}
    </div>
  )
}
