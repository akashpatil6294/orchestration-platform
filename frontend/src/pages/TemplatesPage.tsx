import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { ApiError, api } from '../lib/api'
import { useToast } from '../components/ToastProvider'

interface PackConnection {
  name: string
  kind: string
  label: string
}

interface TemplatePack {
  id: string
  name: string
  category: string
  description: string
  connections_required: PackConnection[]
  step_count: number
  tags: string[]
}

export default function TemplatesPage() {
  const toast = useToast()
  const [packs, setPacks] = useState<TemplatePack[]>([])
  const [loading, setLoading] = useState(true)
  const [installing, setInstalling] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .get<{ items: TemplatePack[] }>('/api/v1/templates')
      .then((data) => setPacks(data.items))
      .catch((cause) => setError(cause instanceof ApiError ? cause.message : 'Could not load templates.'))
      .finally(() => setLoading(false))
  }, [])

  async function install(pack: TemplatePack) {
    setInstalling(pack.id)
    try {
      const workflow = await api.post<{ id: string }>('/api/v1/templates/' + pack.id + '/install')
      toast.success(`Installed “${pack.name}”.`)
      window.location.href = `/workflows/${workflow.id}`
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Could not install the template.')
      setInstalling(null)
    }
  }

  const categories = [...new Set(packs.map((pack) => pack.category))]

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Workflow templates</h1>
          <p className="muted">
            Production-ready packs for engineering and operations. Installing copies the pack into your workflows —
            map the required connections before running.
          </p>
        </div>
      </div>
      {error ? (
        <div className="banner banner-error" role="alert">
          {error}
        </div>
      ) : null}
      {loading ? (
        <p className="muted">Loading…</p>
      ) : (
        categories.map((category) => (
          <section key={category}>
            <h2 className="section-title">{category}</h2>
            <div className="card-grid">
              {packs
                .filter((pack) => pack.category === category)
                .map((pack) => (
                  <article key={pack.id} className="card">
                    <h3>{pack.name}</h3>
                    <p className="muted">{pack.description}</p>
                    <div className="card-meta">
                      <span>{pack.step_count} steps</span>
                      {pack.connections_required.length > 0 ? (
                        <span title={pack.connections_required.map((c) => `${c.name} (${c.kind})`).join(', ')}>
                          Needs {pack.connections_required.length} connection{pack.connections_required.length > 1 ? 's' : ''}
                        </span>
                      ) : (
                        <span>No connections needed</span>
                      )}
                    </div>
                    {pack.connections_required.length > 0 ? (
                      <ul className="muted small">
                        {pack.connections_required.map((conn) => (
                          <li key={conn.name}>
                            <code>{conn.name}</code> — {conn.label}
                          </li>
                        ))}
                      </ul>
                    ) : null}
                    <div className="card-actions">
                      <button
                        type="button"
                        className="button button-primary button-sm"
                        disabled={installing !== null}
                        onClick={() => install(pack)}
                      >
                        {installing === pack.id ? 'Installing…' : 'Install'}
                      </button>
                      <Link className="button button-ghost button-sm" to="/connections">
                        Manage connections
                      </Link>
                    </div>
                  </article>
                ))}
            </div>
          </section>
        ))
      )}
    </div>
  )
}
