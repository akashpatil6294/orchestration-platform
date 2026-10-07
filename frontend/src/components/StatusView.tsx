import type { ReactNode } from 'react'

/** Centred, full-viewport spinner with a label. */
export function FullPageLoader({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="full-page-status" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <p>{label}</p>
    </div>
  )
}

/** Inline loading placeholder for a card or panel. */
export function InlineLoader({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="inline-status" role="status" aria-live="polite">
      <span className="spinner spinner-sm" aria-hidden="true" />
      <span>{label}</span>
    </div>
  )
}

export function ErrorState({
  message,
  onRetry,
  title = 'Something went wrong',
}: {
  message: string
  onRetry?: () => void
  title?: string
}) {
  return (
    <div className="state-card state-card-error" role="alert">
      <h3>{title}</h3>
      <p>{message}</p>
      {onRetry ? (
        <button type="button" className="button button-secondary" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  )
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string
  description: string
  action?: ReactNode
}) {
  return (
    <div className="state-card state-card-empty">
      <h3>{title}</h3>
      <p>{description}</p>
      {action}
    </div>
  )
}

/** A labelled figure used across the dashboard and run pages. */
export function StatCard({
  label,
  value,
  hint,
  tone = 'neutral',
}: {
  label: string
  value: ReactNode
  hint?: string
  tone?: 'neutral' | 'good' | 'warn' | 'bad'
}) {
  return (
    <div className={`stat-card stat-${tone}`}>
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {hint ? <span className="stat-hint">{hint}</span> : null}
    </div>
  )
}

const STATUS_TONES: Record<string, string> = {
  succeeded: 'good',
  success: 'good',
  completed: 'good',
  running: 'active',
  queued: 'pending',
  pending: 'pending',
  scheduled: 'pending',
  retrying: 'warn',
  cancelling: 'warn',
  cancelled: 'muted',
  canceled: 'muted',
  skipped: 'muted',
  failed: 'bad',
  timed_out: 'bad',
  timeout: 'bad',
  error: 'bad',
}

export function StatusBadge({ status }: { status: string }) {
  const tone = STATUS_TONES[status] ?? 'neutral'
  const label = status.replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
  return <span className={`badge badge-${tone}`}>{label}</span>
}

/** Seconds rendered the way an operator reads them. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`
  if (seconds < 60) return `${seconds.toFixed(1)} s`
  const minutes = Math.floor(seconds / 60)
  const remainder = Math.round(seconds % 60)
  if (minutes < 60) return `${minutes}m ${remainder}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return String(value)
  return parsed.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return String(value)
  const deltaSeconds = Math.round((Date.now() - parsed.getTime()) / 1000)
  if (Math.abs(deltaSeconds) < 60) return deltaSeconds <= 0 ? 'just now' : `${deltaSeconds}s ago`
  const minutes = Math.round(deltaSeconds / 60)
  if (Math.abs(minutes) < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (Math.abs(hours) < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

export function ProgressBar({ value }: { value: number }) {
  const percent = Math.max(0, Math.min(100, Math.round(value * 100)))
  return (
    <div className="progress" role="progressbar" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}>
      <div className="progress-fill" style={{ width: `${percent}%` }} />
    </div>
  )
}
