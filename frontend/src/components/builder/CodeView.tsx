/**
 * Code view for the visual builder: read/write the draft as JSON.
 * Two-way sync with the canvas; invalid text never destroys canvas state.
 */
import { useEffect, useState } from 'react'
import type { BuilderDraft } from '../../lib/builder'
import { cloneDraft } from '../../lib/builder'

export default function CodeView(props: {
  draft: BuilderDraft
  onApply: (draft: BuilderDraft) => void
}) {
  const { draft, onApply } = props
  const [text, setText] = useState(() => JSON.stringify(draft, null, 2))
  const [error, setError] = useState<string | null>(null)

  // Refresh the text when the canvas changes the draft underneath us.
  useEffect(() => {
    setText((current) => {
      try {
        const parsed = JSON.parse(current) as BuilderDraft
        // Only overwrite if the user hasn't typed something unparsable.
        if (JSON.stringify(parsed) !== JSON.stringify(draft)) {
          return JSON.stringify(draft, null, 2)
        }
        return current
      } catch {
        return current
      }
    })
  }, [draft])

  const apply = () => {
    try {
      const parsed = JSON.parse(text) as BuilderDraft
      if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.steps)) {
        setError('A draft must be an object with a "steps" array.')
        return
      }
      setError(null)
      onApply({ ...cloneDraft(draft), ...parsed })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Invalid JSON')
    }
  }

  return (
    <div className="code-view">
      <div className="code-view-toolbar">
        <span>Draft JSON — edits apply to the canvas</span>
        <button onClick={apply}>Apply to canvas</button>
      </div>
      {error && (
        <p className="code-view-error" role="alert">{error}</p>
      )}
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        spellCheck={false}
        aria-label="Draft JSON"
        rows={30}
      />
    </div>
  )
}
