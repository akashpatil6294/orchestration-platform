import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'

interface Team {
  id: string
  name: string
  role: string
  member_count: number
  created_at: string
}

interface Member {
  user_id: string
  email?: string
  role: string
}

const ROLES = ['viewer', 'editor', 'operator', 'admin']

export default function TeamsPage() {
  const [teams, setTeams] = useState<Team[]>([])
  const [selected, setSelected] = useState<Team | null>(null)
  const [members, setMembers] = useState<Member[]>([])
  const [newName, setNewName] = useState('')
  const [memberEmail, setMemberEmail] = useState('')
  const [memberRole, setMemberRole] = useState('viewer')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  async function loadTeams() {
    setLoading(true)
    try {
      const { items } = await api.get<{ items: Team[] }>('/api/v1/teams')
      setTeams(items)
      if (selected) {
        const updated = items.find((t) => t.id === selected.id) ?? null
        setSelected(updated)
      }
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not load teams.')
    } finally {
      setLoading(false)
    }
  }

  async function loadMembers(teamId: string) {
    try {
      const data = await api.get<{ items: Member[] }>(`/api/v1/teams/${teamId}/members`)
      setMembers(data.items)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not load members.')
    }
  }

  useEffect(() => {
    loadTeams()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    if (selected) loadMembers(selected.id)
    else setMembers([])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id])

  async function createTeam() {
    if (!newName.trim()) {
      setError('Give the team a name.')
      return
    }
    try {
      await api.post('/api/v1/teams', { name: newName.trim() })
      setNewName('')
      await loadTeams()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not create the team.')
    }
  }

  async function addMember() {
    if (!selected || !memberEmail.trim()) return
    try {
      // Look up the user by email via the members endpoint (backend resolves email).
      await api.put(`/api/v1/teams/${selected.id}/members`, {
        user_id: memberEmail.trim(),
        role: memberRole,
      })
      setMemberEmail('')
      await loadMembers(selected.id)
      await loadTeams()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not add the member.')
    }
  }

  async function changeRole(userId: string, role: string) {
    if (!selected) return
    try {
      await api.put(`/api/v1/teams/${selected.id}/members`, { user_id: userId, role })
      await loadMembers(selected.id)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not change the role.')
    }
  }

  async function removeMember(userId: string) {
    if (!selected || !window.confirm('Remove this member from the team?')) return
    try {
      await api.remove(`/api/v1/teams/${selected.id}/members/${userId}`)
      await loadMembers(selected.id)
      await loadTeams()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not remove the member.')
    }
  }

  const canManage = selected && (selected.role === 'admin' || selected.role === 'owner')

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>Teams</h1>
          <p className="page-subtitle">Share workflows with your team. Roles control who can view, edit, run, or manage.</p>
        </div>
      </header>

      {error && (
        <div className="alert alert-error" role="alert">
          {error}
        </div>
      )}

      <div className="two-col">
        <section className="panel" aria-label="Your teams">
          <h2>Your teams</h2>
          <div className="create-inline">
            <input
              className="input"
              value={newName}
              placeholder="New team name"
              aria-label="New team name"
              onChange={(event) => setNewName(event.target.value)}
            />
            <button type="button" className="button button-primary" onClick={createTeam}>
              Create
            </button>
          </div>
          {loading ? (
            <p className="muted">Loading…</p>
          ) : teams.length === 0 ? (
            <p className="muted">No teams yet.</p>
          ) : (
            <ul className="list">
              {teams.map((team) => (
                <li key={team.id}>
                  <button
                    type="button"
                    className={`list-item${selected?.id === team.id ? ' list-item-active' : ''}`}
                    onClick={() => setSelected(team)}
                  >
                    <span className="list-item-title">{team.name}</span>
                    <span className="list-item-meta">
                      {team.role} · {team.member_count} members
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="panel" aria-label="Team members">
          <h2>{selected ? `Members of ${selected.name}` : 'Members'}</h2>
          {!selected ? (
            <p className="muted">Select a team to manage its members.</p>
          ) : (
            <>
              {canManage && (
                <div className="create-inline">
                  <input
                    className="input"
                    value={memberEmail}
                    placeholder="Member email or user ID"
                    aria-label="Member email or user ID"
                    onChange={(event) => setMemberEmail(event.target.value)}
                  />
                  <select
                    className="input input-sm"
                    value={memberRole}
                    aria-label="Role"
                    onChange={(event) => setMemberRole(event.target.value)}
                  >
                    {ROLES.map((r) => (
                      <option key={r} value={r}>
                        {r}
                      </option>
                    ))}
                  </select>
                  <button type="button" className="button button-secondary" onClick={addMember}>
                    Add
                  </button>
                </div>
              )}
              <table className="table">
                <thead>
                  <tr>
                    <th>User</th>
                    <th>Role</th>
                    {canManage && <th></th>}
                  </tr>
                </thead>
                <tbody>
                  {members.map((member) => (
                    <tr key={member.user_id}>
                      <td>{member.email ?? member.user_id.slice(0, 8)}</td>
                      <td>
                        {canManage ? (
                          <select
                            className="input input-sm"
                            value={member.role}
                            aria-label={`Role for ${member.user_id}`}
                            onChange={(event) => changeRole(member.user_id, event.target.value)}
                          >
                            {ROLES.map((r) => (
                              <option key={r} value={r}>
                                {r}
                              </option>
                            ))}
                          </select>
                        ) : (
                          member.role
                        )}
                      </td>
                      {canManage && (
                        <td>
                          <button
                            type="button"
                            className="button button-danger button-sm"
                            onClick={() => removeMember(member.user_id)}
                          >
                            Remove
                          </button>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="muted">
                Viewer reads · editor edits · operator runs · admin manages the team.
              </p>
            </>
          )}
        </section>
      </div>
    </div>
  )
}
