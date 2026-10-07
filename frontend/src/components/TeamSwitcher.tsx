import { useEffect, useState } from 'react'
import { api } from '../lib/api'

interface Team {
  id: string
  name: string
  role: string
}

export function useTeams() {
  const [teams, setTeams] = useState<Team[]>([])
  const [activeTeamId, setActiveTeamId] = useState<string | null>(() =>
    localStorage.getItem('activeTeamId'),
  )

  useEffect(() => {
    api
      .get<{ items: Team[] }>('/api/v1/teams')
      .then((res) => setTeams(res.items))
      .catch(() => setTeams([]))
  }, [])

  function selectTeam(id: string | null) {
    setActiveTeamId(id)
    if (id) localStorage.setItem('activeTeamId', id)
    else localStorage.removeItem('activeTeamId')
  }

  const activeTeam = teams.find((t) => t.id === activeTeamId) ?? null
  return { teams, activeTeam, activeTeamId, selectTeam }
}

export default function TeamSwitcher() {
  const { teams, activeTeamId, selectTeam } = useTeams()
  if (teams.length === 0) return null

  return (
    <label className="team-switcher">
      <span className="visually-hidden">Active team</span>
      <select
        className="input input-sm"
        value={activeTeamId ?? ''}
        aria-label="Active team"
        onChange={(event) => selectTeam(event.target.value || null)}
        title={activeTeamId ? `Filtering by team` : 'Showing all teams'}
      >
        <option value="">All teams</option>
        {teams.map((team) => (
          <option key={team.id} value={team.id}>
            {team.name} ({team.role})
          </option>
        ))}
      </select>
    </label>
  )
}
