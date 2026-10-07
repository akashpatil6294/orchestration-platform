import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { ApiError, api } from '../lib/api'

interface CompareStep {
  key: string
  type: string
  status: string
  attempts: number
  started_at: string | null
  finished_at: string | null
}

interface RunDetail {
  id: string
  workflow_name: string
  status: string
  created_at: string
  steps: CompareStep[]
}

function durationMs(step: CompareStep): number | null {
  if (!step.started_at || !step.finished_at) return null
  return new Date(step.finished_at).getTime() - new Date(step.started_at).getTime()
}

export default function RunComparePage() {
  const [params] = useSearchParams()
  const [firstId, setFirstId] = useState(params.get('a') ?? '')
  const [secondId, setSecondId] = useState(params.get('b') ?? '')
  const [first, setFirst] = useState<RunDetail | null>(null)
  const [second, setSecond] = useState<RunDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  async function compare() {
    if (!firstId.trim() || !secondId.trim()) {
      setError('Enter two run IDs.')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const [a, b] = await Promise.all([
        api.get<RunDetail>(`/api/v1/runs/${firstId.trim()}`),
        api.get<RunDetail>(`/api/v1/runs/${secondId.trim()}`),
      ])
      setFirst(a)
      setSecond(b)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not load the runs.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (firstId && secondId) compare()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const allKeys = [...new Set([...(first?.steps ?? []).map((s) => s.key), ...(second?.steps ?? []).map((s) => s.key)])]

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Compare runs</h1>
          <p className="muted">Side-by-side step status and duration for two runs of a workflow.</p>
        </div>
      </div>
      {error ? (
        <div className="banner banner-error" role="alert">
          {error}
        </div>
      ) : null}
      <section className="panel">
        <div className="form-row">
          <label className="field">
            <span>Run A</span>
            <input className="input" value={firstId} onChange={(e) => setFirstId(e.target.value)} placeholder="run id" />
          </label>
          <label className="field">
            <span>Run B</span>
            <input className="input" value={secondId} onChange={(e) => setSecondId(e.target.value)} placeholder="run id" />
          </label>
        </div>
        <button type="button" className="button button-primary" disabled={loading} onClick={compare}>
          {loading ? 'Loading…' : 'Compare'}
        </button>
      </section>
      {first && second ? (
        <section className="panel">
          <h2>
            <Link to={`/runs/${first.id}`}>{first.workflow_name}</Link> vs{' '}
            <Link to={`/runs/${second.id}`}>{second.workflow_name}</Link>
          </h2>
          <table className="table">
            <thead>
              <tr>
                <th>Step</th>
                <th>A status</th>
                <th>A duration</th>
                <th>B status</th>
                <th>B duration</th>
              </tr>
            </thead>
            <tbody>
              {allKeys.map((key) => {
                const a = first.steps.find((s) => s.key === key)
                const b = second.steps.find((s) => s.key === key)
                const da = a ? durationMs(a) : null
                const db = b ? durationMs(b) : null
                return (
                  <tr key={key}>
                    <td>
                      <code>{key}</code>
                    </td>
                    <td>{a ? a.status : '—'}</td>
                    <td>{da === null ? '—' : `${(da / 1000).toFixed(1)}s`}</td>
                    <td>{b ? b.status : '—'}</td>
                    <td>{db === null ? '—' : `${(db / 1000).toFixed(1)}s`}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </section>
      ) : null}
    </div>
  )
}
