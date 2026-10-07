/**
 * Test panel for the visual builder: run the draft with sample input,
 * watch node states live, click a node to see its result, pin outputs
 * as sample data for downstream tests.
 */
import { useEffect, useRef, useState } from 'react'
import { api } from '../../lib/api'
import type { BuilderDraft, BuilderStep } from '../../lib/builder'

interface TestRunSummary {
  id: string
  status: string
  is_test: boolean
  total_steps: number
}

interface StepState {
  step_key: string
  status: string
}

export default function TestPanel(props: {
  workflowId: string
  draft: BuilderDraft
  pinnedOutputs: Record<string, unknown>
  onPin: (stepId: string, output: unknown) => void
}) {
  const { workflowId, draft, pinnedOutputs, onPin } = props
  const [sampleInput, setSampleInput] = useState('{}')
  const [run, setRun] = useState<TestRunSummary | null>(null)
  const [stepStates, setStepStates] = useState<StepState[]>([])
  const [selectedStep, setSelectedStep] = useState<BuilderStep | null>(null)
  const [stepDetail, setStepDetail] = useState<Record<string, unknown> | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => () => {
    if (pollTimer.current) clearInterval(pollTimer.current)
  }, [])

  const startTest = async (stepId?: string) => {
    let input: Record<string, unknown> = {}
    try {
      input = JSON.parse(sampleInput) as Record<string, unknown>
    } catch {
      setError('Sample input must be valid JSON.')
      return
    }
    setError(null)
    setRunning(true)
    try {
      const summary = await api.post<TestRunSummary>(`/api/v1/workflows/${workflowId}/test-run`, {
        input,
        step_id: stepId,
        pinned_outputs: pinnedOutputs,
      })
      setRun(summary)
      pollRun(summary.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Test run failed to start')
      setRunning(false)
    }
  }

  const pollRun = (runId: string) => {
    if (pollTimer.current) clearInterval(pollTimer.current)
    const tick = async () => {
      try {
        const detail = await api.get<{ status: string; steps: StepState[] }>(`/api/v1/runs/${runId}`)
        setRun((r) => (r ? { ...r, status: detail.status } : r))
        setStepStates(detail.steps ?? [])
        if (['succeeded', 'failed', 'cancelled'].includes(detail.status)) {
          if (pollTimer.current) clearInterval(pollTimer.current)
          setRunning(false)
        }
      } catch {
        // Poll failures must not kill the panel.
      }
    }
    void tick()
    pollTimer.current = setInterval(tick, 1500)
  }

  const inspectStep = async (step: BuilderStep) => {
    setSelectedStep(step)
    if (!run) {
      setStepDetail(null)
      return
    }
    try {
      const detail = await api.get<{ steps: { step_key: string; output: unknown; input: unknown; attempts: unknown[] }[] }>(
        `/api/v1/runs/${run.id}`,
      )
      const match = detail.steps.find((s) => s.step_key === step.id)
      setStepDetail(match ? { input: match.input, output: match.output, attempts: match.attempts } : { note: 'Step has not run yet.' })
    } catch {
      setStepDetail({ note: 'Could not load step detail.' })
    }
  }

  const statusFor = (id: string) => stepStates.find((s) => s.step_key === id)?.status ?? 'pending'

  return (
    <div className="test-panel">
      <h3>Test run</h3>
      <p className="muted">Runs the current draft without publishing. Test runs never fire triggers, notifications, or quotas.</p>
      <label>
        Sample input (JSON)
        <textarea value={sampleInput} onChange={(e) => setSampleInput(e.target.value)} rows={4} spellCheck={false} />
      </label>
      {error && <p className="code-view-error" role="alert">{error}</p>}
      <div className="test-actions">
        <button onClick={() => startTest()} disabled={running}>{running ? 'Running…' : 'Run draft'}</button>
      </div>

      {run && (
        <p>Run <code>{run.id.slice(0, 8)}</code> · <strong>{run.status}</strong> · {run.total_steps} step(s)</p>
      )}

      <ul className="test-steps">
        {draft.steps.map((step) => (
          <li key={step.id} className={`test-step test-${statusFor(step.id)}`}>
            <button onClick={() => inspectStep(step)} title="Inspect step">
              {step.id} <span className="muted">({step.type})</span> — {statusFor(step.id)}
            </button>
            <button onClick={() => startTest(step.id)} disabled={running} title="Test only this step">
              Test step
            </button>
          </li>
        ))}
      </ul>

      {selectedStep && (
        <div className="test-detail">
          <h4>{selectedStep.id}</h4>
          {stepDetail ? (
            <>
              <pre>{JSON.stringify(stepDetail, null, 2)}</pre>
              <button
                onClick={() => {
                  const output = (stepDetail as { output?: unknown }).output
                  if (output !== undefined) onPin(selectedStep.id, output)
                }}
                disabled={(stepDetail as { output?: unknown }).output === undefined}
              >
                Pin output as sample data
              </button>
            </>
          ) : (
            <p className="muted">No run yet — start a test run first.</p>
          )}
          {pinnedOutputs[selectedStep.id] !== undefined && <p className="muted">✓ Pinned as sample input for downstream tests.</p>}
        </div>
      )}
    </div>
  )
}
