/**
 * The runs list: server-side pagination with search, status/workflow/trigger/
 * date-range filters, sorting, saved filters (localStorage for now) and bulk
 * actions with a per-item result summary.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { ApiError, api } from '../lib/api'
import type { Paginated, RunSummary, WorkflowSummary } from '../lib/types'
import { useConfirm } from '../components/ConfirmDialogProvider'
import { useToast } from '../components/ToastProvider'
import { ActionButton } from '../components/ActionButton'
import { EmptyState, ErrorState, InlineLoader, ProgressBar, StatusBadge, formatDuration, formatRelative } from '../components/StatusView'

const PAGE_SIZE = 25

const STATUS_CHOICES = ['queued', 'running', 'paused', 'cancelling', 'succeeded', 'failed', 'cancelled']
const TRIGGER_CHOICES = ['manual', 'schedule', 'webhook', 'workflow_success', 'subworkflow']

const SORT_CHOICES = [
  { label: 'Newest first', value: 'newest' },
  { label: 'Oldest first', value: 'oldest' },
  { label: 'Longest first', value: 'longest' },
  { label: 'By status', value: 'status' },
]

interface RunFilters {
  search: string
  status: string
  workflowId: string
  trigger: string
  from: string
  to: string
  sort: string
}

const EMPTY_FILTERS: RunFilters = { search: '', status: '', workflowId: '', trigger: '', from: '', to: '', sort: 'newest' }

interface SavedFilter extends RunFilters {
  id: string
  name: string
}

interface ServerSavedFilter {
  id: string
  name: string
  filters: Record<string, string>
}

function toServerFilters(filters: RunFilters): Record<string, string> {
  const out: Record<string, string> = {}
  if (filters.status) out.status = filters.status
  if (filters.workflowId) out.workflow_id = filters.workflowId
  if (filters.trigger) out.trigger = filters.trigger
  if (filters.sort) out.sort = filters.sort
  if (filters.search) out.search = filters.search
  return out
}

function fromServerFilters(entry: ServerSavedFilter): SavedFilter {
  const f = entry.filters
  return {
    id: entry.id,
    name: entry.name,
    search: f.search ?? '',
    status: f.status ?? '',
    workflowId: f.workflow_id ?? '',
    trigger: f.trigger ?? '',
    from: '',
    to: '',
    sort: f.sort ?? 'newest',
  }
}

function buildQuery(filters: RunFilters, offset: number): string {
  const params = new URLSearchParams()
  params.set('limit', String(PAGE_SIZE))
  params.set('offset', String(offset))
  if (filters.search) params.set('search', filters.search)
  if (filters.status) params.set('status', filters.status)
  if (filters.workflowId) params.set('workflow_id', filters.workflowId)
  if (filters.trigger) params.set('trigger', filters.trigger)
  if (filters.from) {
    const fromDate = new Date(`${filters.from}T00:00:00Z`)
    const hours = Math.ceil((Date.now() - fromDate.getTime()) / 3_600_000)
    if (Number.isFinite(hours) && hours >= 1) params.set('window_hours', String(Math.min(hours, 2160)))
  }
  if (filters.to) {
    const toDate = new Date(`${filters.to}T00:00:00Z`)
    toDate.setUTCDate(toDate.getUTCDate() + 1) // inclusive end date -> exclusive bound
    if (!Number.isNaN(toDate.getTime())) params.set('created_before', toDate.toISOString())
  }
  if (filters.sort) params.set('sort', filters.sort)
  return params.toString()
}

interface BulkOutcome {
  succeeded: string[]
  skipped: { id: string; reason: string }[]
  failed: { id: string; reason: string }[]
}

function emptyOutcome(): BulkOutcome {
  return { succeeded: [], skipped: [], failed: [] }
}

export function RunsPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const confirmAction = useConfirm()
  const [searchParams, setSearchParams] = useSearchParams()

  // Deep links (e.g. the dashboard's failed-steps card) pre-filter via the URL.
  const [filters, setFilters] = useState<RunFilters>(() => ({
    ...EMPTY_FILTERS,
    status: searchParams.get('status') ?? '',
    workflowId: searchParams.get('workflow_id') ?? '',
    search: searchParams.get('search') ?? '',
  }))
  const [searchText, setSearchText] = useState(filters.search)
  const [offset, setOffset] = useState(0)
  const [saved, setSaved] = useState<SavedFilter[]>([])
  useEffect(() => {
    api
      .get<{ items: ServerSavedFilter[] }>('/api/v1/saved-filters')
      .then((data) => setSaved(data.items.map(fromServerFilters)))
      .catch(() => {})
  }, [])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [bulkSummary, setBulkSummary] = useState<string | null>(null)
  const [reloadNonce, setReloadNonce] = useState(0)

  useEffect(() => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current)
      if (filters.status) next.set('status', filters.status)
      else next.delete('status')
      if (filters.workflowId) next.set('workflow_id', filters.workflowId)
      else next.delete('workflow_id')
      return next
    })
  }, [filters.status, filters.workflowId, setSearchParams])

  const query = useMemo(() => buildQuery(filters, offset), [filters, offset])
  // Loading is derived: true while the newest query has not settled yet, so no
  // synchronous setState is needed inside the fetch effect.
  const [loaded, setLoaded] = useState<{ query: string; data: Paginated<RunSummary> | null; error: string | null }>({
    query: '',
    data: null,
    error: null,
  })
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([])
  const runs = loaded.query === query ? loaded.data : null
  const error = loaded.query === query ? loaded.error : null
  const loading = loaded.query !== query

  useEffect(() => {
    const controller = new AbortController()
    api
      .get<Paginated<WorkflowSummary>>('/api/v1/workflows?limit=100', controller.signal)
      .then((page) => setWorkflows(page.items))
      .catch(() => setWorkflows([]))
    return () => controller.abort()
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    api
      .get<Paginated<RunSummary>>(`/api/v1/runs?${query}`, controller.signal)
      .then((page) => setLoaded({ query, data: page, error: null }))
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === 'AbortError') return
        setLoaded({ query, data: null, error: cause instanceof Error ? cause.message : 'Could not load runs' })
      })
    return () => controller.abort()
  }, [query, reloadNonce])

  const applyFilters = useCallback((patch: Partial<RunFilters>) => {
    setFilters((current) => ({ ...current, ...patch }))
    setOffset(0)
  }, [])

  const saveCurrentFilters = async () => {
    const name = window.prompt('Name this filter', `Runs ${new Date().toLocaleDateString()}`)
    if (!name) return
    try {
      const created = await api.post<ServerSavedFilter>('/api/v1/saved-filters', {
        name,
        filters: toServerFilters(filters),
      })
      setSaved((current) => [...current.filter((item) => item.name !== name), fromServerFilters(created)])
      toast.success('Filter saved', `“${name}” is available from the saved filters row.`)
    } catch (cause) {
      toast.error('Could not save the filter', cause instanceof ApiError ? cause.message : undefined)
    }
  }

  const applySaved = (entry: SavedFilter) => {
    const { id: _id, name: _name, ...rest } = entry
    setFilters(rest)
    setSearchText(rest.search)
    setOffset(0)
  }

  const deleteSaved = async (entry: SavedFilter) => {
    try {
      await api.remove(`/api/v1/saved-filters/${entry.id}`)
      setSaved((current) => current.filter((item) => item.id !== entry.id))
    } catch (cause) {
      toast.error('Could not delete the filter', cause instanceof ApiError ? cause.message : undefined)
    }
  }

  const page = runs?.offset ?? 0
  const total = runs?.total ?? 0
  const items = runs?.items ?? []

  const toggleRow = (id: string) => {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleAll = () => {
    setSelected((current) => (current.size === items.length ? new Set() : new Set(items.map((item) => item.id))))
  }

  const runBulk = async (action: 'cancel' | 'retry' | 'delete') => {
    const targets = items.filter((item) => selected.has(item.id))
    if (targets.length === 0) return
    const ok = await confirmAction.confirm({
      title: `Bulk ${action}`,
      message: `${action.charAt(0).toUpperCase() + action.slice(1)} ${targets.length} run${targets.length === 1 ? '' : 's'}? Skipped runs (wrong status) are reported individually.`,
      confirmLabel: `${action.charAt(0).toUpperCase() + action.slice(1)} ${targets.length}`,
      danger: action !== 'retry',
    })
    if (!ok) return

    const outcome = emptyOutcome()
    for (const run of targets) {
      const active = ['queued', 'running', 'cancelling', 'paused'].includes(run.status)
      const terminal = ['succeeded', 'failed', 'cancelled'].includes(run.status)
      try {
        if (action === 'cancel') {
          if (!active) {
            outcome.skipped.push({ id: run.id, reason: `status ${run.status}` })
            continue
          }
          await api.post(`/api/v1/runs/${run.id}/cancel`)
        } else if (action === 'retry') {
          if ((run.step_counts?.failed ?? 0) === 0) {
            outcome.skipped.push({ id: run.id, reason: 'no failed steps' })
            continue
          }
          await api.post(`/api/v1/runs/${run.id}/retry`, { steps: [], reset_downstream: true })
        } else {
          if (!terminal) {
            outcome.skipped.push({ id: run.id, reason: 'cancel it first' })
            continue
          }
          await api.remove(`/api/v1/runs/${run.id}`)
        }
        outcome.succeeded.push(run.id)
      } catch (cause) {
        outcome.failed.push({ id: run.id, reason: cause instanceof Error ? cause.message : 'request failed' })
      }
    }

    const parts = [`${outcome.succeeded.length} ${action === 'delete' ? 'deleted' : action === 'cancel' ? 'cancelled' : 'retry scheduled'}`]
    if (outcome.skipped.length) parts.push(`${outcome.skipped.length} skipped`)
    if (outcome.failed.length) parts.push(`${outcome.failed.length} failed`)
    setBulkSummary(parts.join(' · '))
    toast.success(`Bulk ${action} finished`, parts.join(' · '))
    setOffset(0)
    setReloadNonce((value) => value + 1)
    setSelected(new Set())
    if (outcome.failed.length) {
      toast.error('Some runs failed', outcome.failed.map((item) => `${item.id}: ${item.reason}`).join('; '))
    }
  }

  if (error && !runs) return <ErrorState message={error} onRetry={() => setReloadNonce((value) => value + 1)} />

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>Runs</h1>
          <p className="page-subtitle">
            {total} run{total === 1 ? '' : 's'} match the current filters.
          </p>
        </div>
      </header>

      <section className="panel" aria-label="Run filters">
        <form
          className="toolbar"
          role="search"
          onSubmit={(event) => {
            event.preventDefault()
            applyFilters({ search: searchText.trim() })
          }}
        >
          <div className="search-form">
            <input
              className="input"
              type="search"
              placeholder="Search by workflow or run id"
              aria-label="Search runs"
              value={searchText}
              onChange={(event) => setSearchText(event.target.value)}
            />
            <button type="submit" className="button button-secondary">
              Search
            </button>
          </div>
          <label className="field">
            <span>Status</span>
            <select className="input" value={filters.status} onChange={(event) => applyFilters({ status: event.target.value })}>
              <option value="">All statuses</option>
              {STATUS_CHOICES.map((status) => (
                <option key={status} value={status}>
                  {status}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Workflow</span>
            <select className="input" value={filters.workflowId} onChange={(event) => applyFilters({ workflowId: event.target.value })}>
              <option value="">All workflows</option>
              {workflows.map((workflow) => (
                <option key={workflow.id} value={workflow.id}>
                  {workflow.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Trigger</span>
            <select className="input" value={filters.trigger} onChange={(event) => applyFilters({ trigger: event.target.value })}>
              <option value="">All triggers</option>
              {TRIGGER_CHOICES.map((trigger) => (
                <option key={trigger} value={trigger}>
                  {trigger}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>From (UTC)</span>
            <input className="input" type="date" value={filters.from} onChange={(event) => applyFilters({ from: event.target.value })} />
          </label>
          <label className="field">
            <span>To (UTC, inclusive)</span>
            <input className="input" type="date" value={filters.to} onChange={(event) => applyFilters({ to: event.target.value })} />
          </label>
          <label className="field">
            <span>Sort</span>
            <select className="input" value={filters.sort} onChange={(event) => applyFilters({ sort: event.target.value })}>
              {SORT_CHOICES.map((choice) => (
                <option key={choice.value} value={choice.value}>
                  {choice.label}
                </option>
              ))}
            </select>
          </label>
          <div className="row-actions">
            <button type="button" className="button button-ghost" onClick={saveCurrentFilters}>
              Save filter
            </button>
            <button type="button" className="button button-ghost" onClick={() => { setSearchText(''); applyFilters(EMPTY_FILTERS) }}>
              Clear
            </button>
          </div>
        </form>
        {saved.length > 0 ? (
          <div className="chip-row" role="group" aria-label="Saved filters">
            {saved.map((entry) => (
              <span key={entry.id} className="row-actions">
                <button type="button" className="chip" onClick={() => applySaved(entry)}>
                  {entry.name}
                </button>
                <button type="button" className="chip" aria-label={`Delete saved filter ${entry.name}`} onClick={() => deleteSaved(entry)}>
                  ×
                </button>
              </span>
            ))}
          </div>
        ) : null}
      </section>

      {bulkSummary ? (
        <p className="muted" role="status">
          Last bulk action: {bulkSummary}
        </p>
      ) : null}

      {selected.size > 0 ? (
        <div className="bulk-bar" role="toolbar" aria-label="Bulk actions">
          <strong>{selected.size} selected</strong>
          <ActionButton label="Cancel selected" variant="danger" errorTitle="Bulk cancel failed" onAction={() => runBulk('cancel')} />
          <ActionButton label="Retry failed steps" errorTitle="Bulk retry failed" onAction={() => runBulk('retry')} />
          <ActionButton label="Delete selected" variant="danger" errorTitle="Bulk delete failed" onAction={() => runBulk('delete')} />
        </div>
      ) : null}

      <section className="panel">
        {loading && !runs ? (
          <InlineLoader label="Loading runs…" />
        ) : items.length === 0 ? (
          <EmptyState title="No runs found" description="Adjust the filters or start a run from a workflow." />
        ) : (
          <>
            <table className="table">
              <thead>
                <tr>
                  <th scope="col" className="row-select">
                    <input
                      type="checkbox"
                      aria-label="Select all runs on this page"
                      checked={items.length > 0 && selected.size === items.length}
                      onChange={toggleAll}
                    />
                  </th>
                  <th scope="col">Workflow</th>
                  <th scope="col">Status</th>
                  <th scope="col">Trigger</th>
                  <th scope="col">Progress</th>
                  <th scope="col">Duration</th>
                  <th scope="col">Started</th>
                </tr>
              </thead>
              <tbody>
                {items.map((run) => (
                  <tr
                    key={run.id}
                    className="row-click"
                    onClick={(event) => {
                      if ((event.target as HTMLElement).closest('button, a, input')) return
                      navigate(`/runs/${run.id}`)
                    }}
                  >
                    <td className="row-select" onClick={(event) => event.stopPropagation()}>
                      <input type="checkbox" aria-label={`Select run ${run.id}`} checked={selected.has(run.id)} onChange={() => toggleRow(run.id)} />
                    </td>
                    <td>
                      <Link to={`/runs/${run.id}`}>{run.workflow_name || run.workflow_id}</Link>
                      <p className="row-subtitle">v{run.version}</p>
                    </td>
                    <td>
                      <StatusBadge status={run.status} />
                    </td>
                    <td>{run.trigger}</td>
                    <td className="cell-progress">
                      <ProgressBar value={run.progress} />
                      <span className="muted">
                        {run.completed_steps}/{run.total_steps}
                      </span>
                    </td>
                    <td>{formatDuration(run.duration_seconds)}</td>
                    <td>{formatRelative(run.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="toolbar" style={{ marginTop: '0.8rem' }}>
              <span className="muted">
                Showing {page + 1}–{page + items.length} of {total}
              </span>
              <span className="row-actions">
                <button type="button" className="button button-ghost" disabled={page === 0 || loading} onClick={() => setOffset(Math.max(0, page - PAGE_SIZE))}>
                  Previous
                </button>
                <button
                  type="button"
                  className="button button-ghost"
                  disabled={!runs?.has_more || loading}
                  onClick={() => setOffset(page + PAGE_SIZE)}
                >
                  Next
                </button>
              </span>
            </div>
          </>
        )}
      </section>
    </div>
  )
}

export default RunsPage
