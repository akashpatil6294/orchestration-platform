import { useMemo } from 'react'

interface TimelineStep {
  key: string
  type: string
  status: string
  started_at: string | null
  finished_at: string | null
  attempts: number
}

const STATUS_COLORS: Record<string, string> = {
  succeeded: 'var(--status-ok)',
  failed: 'var(--status-error)',
  running: 'var(--status-running)',
  cancelled: 'var(--status-muted)',
  pending: 'var(--status-pending)',
  retrying: 'var(--status-warning)',
  skipped: 'var(--status-muted)',
}

interface TimelineBar {
  step: TimelineStep
  left: number
  width: number
  seconds: string
}

function computeBars(steps: TimelineStep[]): TimelineBar[] | null {
  const timed = steps.filter((s) => s.started_at)
  if (timed.length === 0) return null
  const now = Date.now()
  const starts = timed.map((s) => new Date(s.started_at as string).getTime())
  const ends = timed.map((s) => (s.finished_at ? new Date(s.finished_at).getTime() : now))
  const min = Math.min(...starts)
  const max = Math.max(...ends, min + 1)
  const span = max - min
  return timed.map((step) => {
    const start = new Date(step.started_at as string).getTime()
    const end = step.finished_at ? new Date(step.finished_at).getTime() : now
    return {
      step,
      left: ((start - min) / span) * 100,
      width: Math.max(((end - start) / span) * 100, 0.8),
      seconds: ((end - start) / 1000).toFixed(1),
    }
  })
}

/** Gantt-style step timeline for a run. Pure CSS, no chart library. */
export default function RunTimeline({ steps }: { steps: TimelineStep[] }) {
  const bars = useMemo(() => computeBars(steps), [steps])

  if (!bars) {
    return <p className="muted">No timing data yet.</p>
  }

  return (
    <div className="timeline" role="img" aria-label="Step execution timeline">
      {bars.map(({ step, left, width, seconds }) => (
        <div key={step.key} className="timeline-row">
          <div className="timeline-label" title={`${step.key} (${step.type})`}>
            {step.key}
          </div>
          <div className="timeline-track">
            <div
              className="timeline-bar"
              style={{
                left: `${left}%`,
                width: `${width}%`,
                backgroundColor: STATUS_COLORS[step.status] ?? 'var(--status-muted)',
              }}
              title={`${step.key}: ${step.status}, ${seconds}s${step.attempts > 1 ? `, ${step.attempts} attempts` : ''}`}
            />
          </div>
          <div className="timeline-duration muted small">{seconds}s</div>
        </div>
      ))}
    </div>
  )
}
