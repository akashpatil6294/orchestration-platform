import { useEffect, useState } from 'react'

import { ApiError, api } from '../lib/api'
import { useToast } from '../components/ToastProvider'

interface Connection {
  id: string
  name: string
  kind: string
  team_id: string | null
  owner_id: string
  created_at: string
}

interface Team {
  id: string
  name: string
}

const KIND_LABELS: Record<string, string> = {
  slack_webhook: 'Slack webhook',
  sql_url: 'SQL connection URL',
  generic: 'Generic secret',
}

export default function ConnectionsPage() {
  const toast = useToast()
  const [connections, setConnections] = useState<Connection[]>([])
  const [teams, setTeams] = useState<Team[]>([])
  const [kinds, setKinds] = useState<string[]>(['generic'])
  const [loading, setLoading] = useState(true)
  const [name, setName] = useState('')
  const [kind, setKind] = useState('generic')
  const [value, setValue] = useState('')
  const [teamId, setTeamId] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function load() {
    setLoading(true)
    try {
      const [connData, teamData, kindData] = await Promise.all([
        api.get<{ items: Connection[] }>('/api/v1/connections'),
        api.get<{ items: Team[] }>('/api/v1/teams').catch(() => ({ items: [] as Team[] })),
        api.get<{ kinds: string[] }>('/api/v1/connections/kinds').catch(() => ({ kinds: ['generic'] })),
      ])
      setConnections(connData.items)
      setTeams(teamData.items)
      setKinds(kindData.kinds)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not load connections.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function create() {
    if (!name.trim() || !value.trim()) {
      setError('Give the connection a name and a value.')
      return
    }
    setSaving(true)
    setError(null)
    try {
      await api.post('/api/v1/connections', {
        name: name.trim(),
        kind,
        value: value.trim(),
        team_id: teamId || null,
      })
      setName('')
      setValue('')
      setTeamId('')
      toast.success('Connection saved. Its value is never shown again.')
      await load()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not save the connection.')
    } finally {
      setSaving(false)
    }
  }

  async function remove(connection: Connection) {
    if (!window.confirm(`Delete connection “${connection.name}”? Workflows referencing it will fail.`)) return
    try {
      await api.remove(`/api/v1/connections/${connection.id}`)
      toast.success('Connection deleted.')
      await load()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not delete the connection.')
    }
  }

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Connections</h1>
          <p className="muted">
            Named credentials your workflows share. Reference one from a step with <code>{'{"$connection": "name"}'}</code> —
            the value is decrypted only when a worker executes that step, and never shown here.
          </p>
        </div>
      </div>
      {error ? (
        <div className="banner banner-error" role="alert">
          {error}
        </div>
      ) : null}
      <section className="panel">
        <h2>New connection</h2>
        <div className="form-row">
          <label className="field">
            <span>Name</span>
            <input className="input" value={name} maxLength={120} placeholder="team-slack" onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="field">
            <span>Kind</span>
            <select className="input" value={kind} onChange={(e) => setKind(e.target.value)}>
              {kinds.map((k) => (
                <option key={k} value={k}>
                  {KIND_LABELS[k] ?? k}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label className="field">
          <span>Value (write-only)</span>
          <input
            className="input"
            type="password"
            value={value}
            placeholder={kind === 'slack_webhook' ? 'https://hooks.slack.com/…' : 'The credential value'}
            onChange={(e) => setValue(e.target.value)}
          />
        </label>
        {teams.length > 0 ? (
          <label className="field">
            <span>Share with team (optional)</span>
            <select className="input" value={teamId} onChange={(e) => setTeamId(e.target.value)}>
              <option value="">Just me</option>
              {teams.map((team) => (
                <option key={team.id} value={team.id}>
                  {team.name}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <button type="button" className="button button-primary" disabled={saving} onClick={create}>
          {saving ? 'Saving…' : 'Save connection'}
        </button>
      </section>
      <section className="panel">
        <h2>Saved connections</h2>
        {loading ? (
          <p className="muted">Loading…</p>
        ) : connections.length === 0 ? (
          <p className="muted">No connections yet.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Kind</th>
                <th>Shared with</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {connections.map((connection) => (
                <tr key={connection.id}>
                  <td>
                    <code>{connection.name}</code>
                  </td>
                  <td>{KIND_LABELS[connection.kind] ?? connection.kind}</td>
                  <td>{connection.team_id ? teams.find((t) => t.id === connection.team_id)?.name ?? 'a team' : 'Just me'}</td>
                  <td className="table-actions">
                    <button type="button" className="button button-ghost button-sm button-danger" onClick={() => remove(connection)}>
                      Delete
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
