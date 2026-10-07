/**
 * Publish flow: modal with the diff against the latest version, validation
 * summary, a required change note, and schedule/trigger handling.
 */
import { useEffect, useState } from 'react'
import { api, ApiError } from '../../lib/api'
import type { ValidationIssue } from '../../lib/builder'

interface DiffResult {
  from: string
  to: string
  steps_added: string[]
  steps_removed: string[]
  steps_changed: { step_id: string; fields: { field: string; old: unknown; new: unknown }[] }[]
  edge_changes: { step_id: string; removed_edges: string[]; added_edges: string[] }[]
  workflow_changes: { field: string; old: unknown; new: unknown }[]
  has_changes: boolean
}

export default function PublishModal(props: {
  workflowId: string
  latestVersion: number
  issues: ValidationIssue[]
  onClose: () => void
  onPublished: (version: number) => void
}) {
  const { workflowId, latestVersion, issues, onClose, onPublished } = props
  const [diff, setDiff] = useState<DiffResult | null>(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const params = latestVersion > 0 ? `?from_version=version:${latestVersion}&to_version=draft` : ''
    api
      .get<DiffResult>(`/api/v1/workflows/${workflowId}/diff${params}`)
      .then((d) => {
        if (!cancelled) setDiff(d)
      })
      .catch(() => {
        if (!cancelled) setError('Could not load the diff.')
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowId, latestVersion])

  const errors = issues.filter((i) => !i.code.startsWith('graph.'))

  const publish = async () => {
    if (!note.trim()) {
      setError('A change note is required.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const result = await api.post<{ version: number }>(`/api/v1/workflows/${workflowId}/publish`, { note: note.trim() })
      onPublished(result.version)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Publish failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Publish workflow">
      <div className="modal modal-wide">
        <h2>Publish version {latestVersion + 1}</h2>

        {errors.length > 0 ? (
          <p className="code-view-error" role="alert">
            {errors.length} validation error(s) block publishing. Fix them in the canvas first.
          </p>
        ) : (
          <p className="muted">Validation passed.</p>
        )}

        <h3>Changes since v{latestVersion || '—'}</h3>
        {!diff ? (
          <p className="muted">Loading diff…</p>
        ) : !diff.has_changes ? (
          <p className="muted">No changes since the last published version.</p>
        ) : (
          <ul className="diff-list">
            {diff.steps_added.map((id) => (
              <li key={`add-${id}`} className="diff-added">+ step {id}</li>
            ))}
            {diff.steps_removed.map((id) => (
              <li key={`del-${id}`} className="diff-removed">− step {id}</li>
            ))}
            {diff.steps_changed.map((c) => (
              <li key={`chg-${c.step_id}`} className="diff-changed">
                ~ step {c.step_id}: {c.fields.map((f) => f.field).join(', ')}
              </li>
            ))}
            {diff.edge_changes.map((c) => (
              <li key={`edge-${c.step_id}`} className="diff-changed">
                ~ edges on {c.step_id}
                {c.removed_edges.length > 0 && ` (−${c.removed_edges.join(', ')})`}
                {c.added_edges.length > 0 && ` (+${c.added_edges.join(', ')})`}
              </li>
            ))}
          </ul>
        )}

        <label>
          Change note (required)
          <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="What changed in this version?" />
        </label>

        {error && <p className="code-view-error" role="alert">{error}</p>}

        <div className="modal-actions">
          <button onClick={onClose} className="btn-ghost">Cancel</button>
          <button onClick={publish} disabled={busy || errors.length > 0 || !diff?.has_changes}>
            {busy ? 'Publishing…' : `Publish v${latestVersion + 1}`}
          </button>
        </div>
      </div>
    </div>
  )
}
