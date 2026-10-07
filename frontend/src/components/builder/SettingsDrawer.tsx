/**
 * Workflow settings drawer: timeout, SLA, on_failure lane, default queue,
 * and the run-parameter input schema (rendered as a form in the Run dialog).
 */
import { useState } from 'react'
import type { BuilderDraft, BuilderStep } from '../../lib/builder'
import { cloneDraft, newStepId } from '../../lib/builder'

export default function SettingsDrawer(props: {
  draft: BuilderDraft
  onChange: (draft: BuilderDraft) => void
  onClose: () => void
}) {
  const { draft, onChange, onClose } = props
  const [newFailureType, setNewFailureType] = useState('slack.post')

  const set = (patch: Partial<BuilderDraft>) => onChange({ ...cloneDraft(draft), ...patch })

  const addFailureStep = () => {
    const next = cloneDraft(draft)
    const step: BuilderStep = { id: newStepId('on_failure'), type: newFailureType, input: {}, depends_on: [] }
    next.on_failure = [...(next.on_failure ?? []), step]
    onChange(next)
  }

  const removeFailureStep = (id: string) => {
    const next = cloneDraft(draft)
    next.on_failure = (next.on_failure ?? []).filter((s) => s.id !== id)
    onChange(next)
  }

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <div className="drawer" role="dialog" aria-modal="true" aria-label="Workflow settings" onClick={(e) => e.stopPropagation()}>
        <h2>Workflow settings</h2>

        <label>
          Timeout (seconds)
          <input
            type="number" min={1}
            value={draft.timeout_seconds ?? ''}
            placeholder="No workflow timeout"
            onChange={(e) => set({ timeout_seconds: e.target.value ? Number(e.target.value) : undefined })}
          />
        </label>

        <label>
          SLA (seconds)
          <input
            type="number" min={1}
            value={draft.sla_seconds ?? ''}
            placeholder="No SLA"
            onChange={(e) => set({ sla_seconds: e.target.value ? Number(e.target.value) : undefined })}
          />
        </label>

        <label>
          Default queue
          <input
            type="text"
            value={draft.default_queue ?? 'default'}
            onChange={(e) => set({ default_queue: e.target.value })}
          />
        </label>

        <label>
          Max parallel steps
          <input
            type="number" min={1} max={64}
            value={draft.default_max_parallel ?? 4}
            onChange={(e) => set({ default_max_parallel: Number(e.target.value) })}
          />
        </label>

        <section>
          <h3>On-failure lane</h3>
          <p className="muted">Steps here run when the workflow fails. Shown as a distinct lane in the run view.</p>
          <ul className="failure-lane">
            {(draft.on_failure ?? []).map((s) => (
              <li key={s.id}>
                <code>{s.id}</code> <span className="muted">{s.type}</span>
                <button onClick={() => removeFailureStep(s.id)} aria-label={`Remove ${s.id}`}>×</button>
              </li>
            ))}
          </ul>
          <div className="failure-add">
            <input value={newFailureType} onChange={(e) => setNewFailureType(e.target.value)} aria-label="Failure step task type" list="task-type-list" />
            <button onClick={addFailureStep}>Add failure step</button>
          </div>
        </section>

        <div className="modal-actions">
          <button onClick={onClose} className="btn-ghost">Close</button>
        </div>
      </div>
    </div>
  )
}
