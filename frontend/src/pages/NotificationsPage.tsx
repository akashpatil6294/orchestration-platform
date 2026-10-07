import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'

interface Channel {
  id: string
  name: string
  channel_type: string
  workflow_id: string | null
  events: string[]
  is_active: boolean
  created_at: string
}

interface Delivery {
  id: string
  channel_id: string
  event: string
  status: string
  status_code: number | null
  error: string | null
  created_at: string
}

const CHANNEL_TYPES = [
  { value: 'webhook', label: 'Webhook', hint: 'https://example.com/hooks/orchestrator' },
  { value: 'slack', label: 'Slack', hint: 'https://hooks.slack.com/services/…' },
  { value: 'email', label: 'Email', hint: 'ops@example.com' },
]

const ALL_EVENTS = ['run.started', 'run.succeeded', 'run.failed', 'run.cancelled', 'run.waiting_approval']

export default function NotificationsPage() {
  const [channels, setChannels] = useState<Channel[]>([])
  const [deliveries, setDeliveries] = useState<Delivery[]>([])
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [channelType, setChannelType] = useState('webhook')
  const [events, setEvents] = useState<string[]>(['run.failed'])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function load() {
    setLoading(true)
    try {
      const [channelData, deliveryData] = await Promise.all([
        api.get<{ items: Channel[] }>('/api/v1/notifications/channels'),
        api.get<{ items: Delivery[] }>('/api/v1/notifications/deliveries').catch(() => ({ items: [] as Delivery[] })),
      ])
      setChannels(channelData.items)
      setDeliveries(deliveryData.items)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not load channels.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  function toggleEvent(event: string) {
    setEvents((prev) => (prev.includes(event) ? prev.filter((e) => e !== event) : [...prev, event]))
  }

  async function create() {
    if (!name.trim() || !url.trim() || events.length === 0) {
      setError('Give the channel a name, a webhook URL, and at least one event.')
      return
    }
    setSaving(true)
    setError(null)
    try {
      await api.post('/api/v1/notifications/channels', {
        name: name.trim(),
        url: url.trim(),
        events,
        channel_type: channelType,
      })
      setName('')
      setUrl('')
      setChannelType('webhook')
      setEvents(['run.failed'])
      await load()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not create the channel.')
    } finally {
      setSaving(false)
    }
  }

  async function remove(id: string) {
    if (!window.confirm('Delete this notification channel?')) return
    try {
      await api.remove(`/api/v1/notifications/channels/${id}`)
      await load()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not delete the channel.')
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>Notifications</h1>
          <p className="page-subtitle">
            Webhook endpoints that receive signed JSON payloads when runs change state.
          </p>
        </div>
      </header>

      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}

      <section className="panel" aria-label="Add notification channel">
        <h2>Add channel</h2>
        <div className="form-grid">
          <label className="field">
            <span>Name</span>
            <input
              className="input"
              value={name}
              placeholder="Ops channel"
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label className="field">
            <span>Type</span>
            <select className="input" value={channelType} onChange={(event) => setChannelType(event.target.value)}>
              {CHANNEL_TYPES.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field field-wide">
            <span>{channelType === 'email' ? 'Recipient address' : channelType === 'slack' ? 'Slack webhook URL' : 'Webhook URL (HTTPS)'}</span>
            <input
              className="input"
              value={url}
              placeholder={CHANNEL_TYPES.find((t) => t.value === channelType)?.hint}
              onChange={(event) => setUrl(event.target.value)}
            />
          </label>
        </div>
        <div className="scope-selector" role="group" aria-label="Subscribed events">
          {ALL_EVENTS.map((event) => (
            <label key={event} className="scope-option">
              <input type="checkbox" checked={events.includes(event)} onChange={() => toggleEvent(event)} />
              <code>{event}</code>
            </label>
          ))}
        </div>
        <div className="form-actions">
          <button type="button" className="button button-primary" onClick={create} disabled={saving}>
            {saving ? 'Adding…' : 'Add channel'}
          </button>
        </div>
        <p className="muted">
          Payloads carry an <code>X-Orchestrator-Signature</code> HMAC header so your endpoint can verify
          they came from this platform.
        </p>
      </section>

      <section className="panel" aria-label="Notification channels">
        <h2>Channels</h2>
        {loading ? (
          <p className="muted">Loading…</p>
        ) : channels.length === 0 ? (
          <p className="muted">No channels yet.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Events</th>
                <th>Created</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {channels.map((channel) => (
                <tr key={channel.id}>
                  <td>{channel.name}</td>
                  <td>
                    <code className="tag">{channel.channel_type}</code>
                  </td>
                  <td>
                    {channel.events.map((e) => (
                      <code key={e} className="tag">
                        {e}
                      </code>
                    ))}
                  </td>
                  <td>{new Date(channel.created_at).toLocaleDateString()}</td>
                  <td>
                    <button type="button" className="button button-danger button-sm" onClick={() => remove(channel.id)}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="panel" aria-label="Recent deliveries">
        <h2>Recent deliveries</h2>
        {deliveries.length === 0 ? (
          <p className="muted">No deliveries yet.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Event</th>
                <th>Status</th>
                <th>Detail</th>
                <th>At</th>
              </tr>
            </thead>
            <tbody>
              {deliveries.slice(0, 20).map((delivery) => (
                <tr key={delivery.id}>
                  <td>
                    <code>{delivery.event}</code>
                  </td>
                  <td>
                    <span className={delivery.status === 'delivered' ? 'status-ok' : 'status-error'}>
                      {delivery.status}
                    </span>
                  </td>
                  <td className="muted small">
                    {delivery.status_code ? `HTTP ${delivery.status_code}` : ''}
                    {delivery.error ? ` — ${delivery.error}` : ''}
                  </td>
                  <td>{new Date(delivery.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}
