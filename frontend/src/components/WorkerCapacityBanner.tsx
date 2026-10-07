import { useCallback, useState } from 'react'

import { api } from '../lib/api'
import { useLiveResource } from '../lib/useLiveResource'
import { useResource } from '../lib/useResource'
import { useToast } from './ToastProvider'

interface SessionInfo {
  user: { is_admin: boolean }
}

interface CapacityQueue {
  queued_steps: number
  oldest_queued_age_seconds: number
  task_types: string[]
}

interface CapacityResponse {
  active_workers: number
  queues: Record<string, CapacityQueue>
  task_types: Record<string, { covered: boolean; queues: string[] }>
  embedded_worker_allowed: boolean
  queue_wait_warning_seconds: number
}

interface Props {
  /** When set, only warn about these queue/task-type pairs (run detail view). */
  scope?: { queue: string; taskType: string }[]
}

function pairsInScope(scope: Props['scope'], queue: string, taskType: string): boolean {
  if (!scope) return true
  return scope.some((item) => item.queue === queue && item.taskType === taskType)
}

export function WorkerCapacityBanner({ scope }: Props) {
  const session = useResource<SessionInfo>((signal) => api.get<SessionInfo>('/api/v1/auth/session', signal), [])
  const isAdmin = session.data?.user?.is_admin ?? false
  const toast = useToast()
  const [starting, setStarting] = useState(false)
  const [minting, setMinting] = useState(false)
  const [mintedToken, setMintedToken] = useState<{ workerId: string; token: string } | null>(null)
  const capacity = useLiveResource<CapacityResponse>(
    (signal) => api.get<CapacityResponse>('/api/v1/ops/capacity', signal),
    [],
    15000,
    true,
  )
  const data = capacity.data

  const startEmbedded = useCallback(async () => {
    setStarting(true)
    try {
      const result = await api.post<{ worker_id: string }>('/api/v1/ops/embedded-worker/start', {})
      toast.success(`Embedded worker started (${result.worker_id})`)
      capacity.refresh()
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not start the embedded worker')
    } finally {
      setStarting(false)
    }
  }, [capacity, toast])

  // Admin-only: mint a worker credential on explicit click. The plaintext
  // token is returned exactly once and shown in a dismissible panel; the
  // copy button below prefills it into a working start command.
  const mintWorkerToken = useCallback(async () => {
    setMinting(true)
    try {
      const workerId = `worker-${Date.now().toString(36)}`
      const result = await api.post<{ worker_id: string; token: string }>('/api/v1/workers', {
        worker_id: workerId,
        queues: ['default'],
      })
      setMintedToken({ workerId: result.worker_id, token: result.token })
      toast.success('Worker credential created — copy the token now, it will not be shown again')
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not create a worker credential')
    } finally {
      setMinting(false)
    }
  }, [toast])

  const copyWorkerCommand = useCallback(async () => {
    const command = mintedToken
      ? `WORKER_ID=${mintedToken.workerId} WORKER_TOKEN=${mintedToken.token} python -m app.sample_worker`
      : 'python -m app.cli create-worker --worker-id my-worker # prints a WORKER_TOKEN\nWORKER_TOKEN=<paste-token> WORKER_ID=my-worker python -m app.sample_worker'
    try {
      await navigator.clipboard.writeText(command)
      toast.success('Worker start command copied')
    } catch {
      toast.error('Could not access the clipboard')
    }
  }, [mintedToken, toast])

  if (!data || !data.queues || !data.task_types) return null
  const stuck: { queue: string; taskType: string; age: number }[] = []
  for (const [queue, info] of Object.entries(data.queues)) {
    if (info.oldest_queued_age_seconds < data.queue_wait_warning_seconds) continue
    for (const taskType of info.task_types) {
      const coverage = data.task_types[taskType]
      if (coverage && !coverage.covered && pairsInScope(scope, queue, taskType)) {
        stuck.push({ queue, taskType, age: info.oldest_queued_age_seconds })
      }
    }
  }
  if (stuck.length === 0) return null
  const first = stuck[0]
  const more = stuck.length - 1

  return (
    <section className="banner banner-warn" role="alert" aria-label="No worker available">
      <div>
        <strong>No worker is available</strong>
        <span>
          {' '}
          for queue <code>{first.queue}</code> / task type <code>{first.taskType}</code>
          {more > 0 ? ` (+${more} more)` : ''}. Steps have been waiting {Math.round(first.age)}s.
        </span>
      </div>
      <div className="banner-actions">
        {isAdmin && data.embedded_worker_allowed ? (
          <button type="button" className="button button-primary" disabled={starting} onClick={startEmbedded}>
            {starting ? 'Starting…' : 'Start embedded worker'}
          </button>
        ) : null}
        {isAdmin ? (
          <button type="button" className="button button-secondary" disabled={minting} onClick={mintWorkerToken}>
            {minting ? 'Creating…' : 'Generate worker token'}
          </button>
        ) : null}
        <button type="button" className="button button-secondary" onClick={copyWorkerCommand}>
          {mintedToken ? 'Copy start command (token prefilled)' : 'Copy worker command'}
        </button>
      </div>
      {mintedToken ? (
        <div className="token-once" role="status">
          <div>
            <strong>Token for {mintedToken.workerId} (shown once):</strong>{' '}
            <code>{mintedToken.token}</code>
          </div>
          <div className="muted">Copy it now — it cannot be retrieved again. The copy button above prefills it.</div>
          <button type="button" className="button button-ghost" onClick={() => setMintedToken(null)}>
            Dismiss
          </button>
        </div>
      ) : null}
    </section>
  )
}
