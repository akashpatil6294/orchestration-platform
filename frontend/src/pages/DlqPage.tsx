/**
 * Dead-letter queue: failed steps with their error details, plus redrive.
 *
 * Redrive is idempotent server-side; an `already_redriven` response is surfaced
 * as a neutral notice instead of an error. Bulk redrive reports per-item
 * outcomes.
 */
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../lib/api'
import type { DlqEntry, RedriveResult } from '../lib/types'
import { useConfirm } from '../components/ConfirmDialogProvider'
import { useToast } from '../components/ToastProvider'
import { ActionButton } from '../components/ActionButton'
import { EmptyState, ErrorState, InlineLoader, formatRelative } from '../components/StatusView'

interface DlqResponse {
  items: DlqEntry[]
}

export function DlqPage() {
  const toast = useToast()
  const confirmAction = useConfirm()
  const [items, setItems] = useState<DlqEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [expanded, setExpanded] = useState<string | null>(null)

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const response = await api.get<DlqResponse>('/api/v1/dlq?limit=200', signal)
      setItems(response.items)
      setError(null)
      setSelected(new Set())
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === 'AbortError') return
      setError(cause instanceof Error ? cause.message : 'Could not load dead letters')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    api
      .get<DlqResponse>('/api/v1/dlq?limit=200', controller.signal)
      .then((response) => {
        setItems(response.items)
        setError(null)
        setSelected(new Set())
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === 'AbortError') return
        setError(cause instanceof Error ? cause.message : 'Could not load dead letters')
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [])

  const refresh = useCallback(() => {
    setLoading(true)
    load()
  }, [load])

  const redrive = async (entry: DlqEntry) => {
    const result = await api.post<RedriveResult>(`/api/v1/dlq/${entry.step_run_id}/redrive`)
    if (result.already_redriven) {
      toast.info('Already scheduled', `Step "${entry.step_key}" was redriven earlier and is ${result.status}.`)
    } else {
      toast.success('Redrive scheduled', `Step "${entry.step_key}" will run again (run ${result.status}).`)
    }
    load()
  }

  const redriveBulk = async () => {
    const targets = (items ?? []).filter((item) => selected.has(item.step_run_id))
    if (targets.length === 0) return
    const ok = await confirmAction.confirm({
      title: 'Bulk redrive',
      message: `Schedule another attempt for ${targets.length} failed step${targets.length === 1 ? '' : 's'}? Steps already scheduled are skipped.`,
      confirmLabel: 'Redrive all',
    })
    if (!ok) return
    let scheduled = 0
    let already = 0
    let failed = 0
    for (const entry of targets) {
      try {
        const result = await api.post<RedriveResult>(`/api/v1/dlq/${entry.step_run_id}/redrive`)
        if (result.already_redriven) already += 1
        else scheduled += 1
      } catch {
        failed += 1
      }
    }
    const parts = [`${scheduled} scheduled`]
    if (already) parts.push(`${already} already scheduled`)
    if (failed) parts.push(`${failed} failed`)
    toast.success('Bulk redrive finished', parts.join(' · '))
    load()
  }

  if (error && !items) return <ErrorState message={error} onRetry={() => load()} />

  const visible = (items ?? []).filter((entry) => {
    if (!filter) return true
    const needle = filter.toLowerCase()
    return (
      entry.workflow_name.toLowerCase().includes(needle) ||
      entry.step_key.toLowerCase().includes(needle) ||
      entry.task_type.toLowerCase().includes(needle) ||
      entry.queue.toLowerCase().includes(needle)
    )
  })

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>Dead letters</h1>
          <p className="page-subtitle">Failed steps waiting for another attempt or for diagnosis.</p>
        </div>
        <div className="live-controls">
          <input
            className="input"
            type="search"
            placeholder="Filter by workflow, step, type or queue"
            aria-label="Filter dead letters"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />
          <button type="button" className="button button-secondary" onClick={refresh} disabled={loading}>
            {loading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </header>

      {selected.size > 0 ? (
        <div className="bulk-bar" role="toolbar" aria-label="Bulk redrive">
          <strong>{selected.size} selected</strong>
          <ActionButton label="Redrive selected" errorTitle="Bulk redrive failed" onAction={redriveBulk} />
        </div>
      ) : null}

      <section className="panel">
        {loading && !items ? (
          <InlineLoader label="Loading dead letters…" />
        ) : visible.length === 0 ? (
          <EmptyState
            title={items && items.length > 0 ? 'Nothing matches that filter' : 'No dead letters'}
            description={
              items && items.length > 0
                ? 'Clear the filter to see every failed step.'
                : 'Failed steps appear here after their retry budget is exhausted.'
            }
          />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th scope="col" className="row-select">
                  <input
                    type="checkbox"
                    aria-label="Select all dead letters"
                    checked={selected.size === visible.length && visible.length > 0}
                    onChange={() =>
                      setSelected((current) => (current.size === visible.length ? new Set() : new Set(visible.map((entry) => entry.step_run_id))))
                    }
                  />
                </th>
                <th scope="col">Workflow</th>
                <th scope="col">Step</th>
                <th scope="col">Attempts</th>
                <th scope="col">Queue</th>
                <th scope="col">Failed</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {visible.map((entry) => (
                <tr key={entry.step_run_id}>
                  <td className="row-select">
                    <input
                      type="checkbox"
                      aria-label={`Select step ${entry.step_key}`}
                      checked={selected.has(entry.step_run_id)}
                      onChange={() =>
                        setSelected((current) => {
                          const next = new Set(current)
                          if (next.has(entry.step_run_id)) next.delete(entry.step_run_id)
                          else next.add(entry.step_run_id)
                          return next
                        })
                      }
                    />
                  </td>
                  <td>
                    <Link to={`/runs/${entry.run_id}`}>{entry.workflow_name}</Link>
                  </td>
                  <td>
                    <strong>{entry.step_key}</strong>
                    <p className="row-subtitle">
                      {entry.task_type} · priority {entry.priority}
                      {entry.lease_expirations > 0 ? ` · ${entry.lease_expirations} lease expirations` : ''}
                    </p>
                  </td>
                  <td>
                    {entry.attempts}/{entry.retry_limit}
                    {entry.error ? (
                      <>
                        {' '}
                        <button type="button" className="button button-ghost" aria-expanded={expanded === entry.step_run_id} onClick={() => setExpanded((current) => (current === entry.step_run_id ? null : entry.step_run_id))}>
                          {expanded === entry.step_run_id ? 'Hide error' : 'Show error'}
                        </button>
                        {expanded === entry.step_run_id ? (
                          <pre className="code-block code-block-logs">{JSON.stringify(entry.error, null, 2)}</pre>
                        ) : null}
                      </>
                    ) : null}
                  </td>
                  <td>{entry.queue}</td>
                  <td>{formatRelative(entry.failed_at)}</td>
                  <td>
                    <span className="row-actions">
                      <ActionButton label="Redrive" variant="primary" errorTitle="Redrive failed" onAction={() => redrive(entry)} />
                      <Link className="button button-ghost" to={`/runs/${entry.run_id}`}>
                        Open run
                      </Link>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}

export default DlqPage
