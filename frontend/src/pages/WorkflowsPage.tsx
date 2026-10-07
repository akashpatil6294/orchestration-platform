import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { ApiError, api } from '../lib/api'
import type { Paginated, WorkflowSummary } from '../lib/types'
import { useResource } from '../lib/useResource'
import { EmptyState, ErrorState, InlineLoader, StatusBadge, formatRelative } from '../components/StatusView'

/** A minimal, publishable starter graph so a new workflow is never empty. */
function starterDefinition(name: string) {
  return {
    name,
    description: 'Describe what this workflow does.',
    default_max_parallel: 2,
    tags: [],
    steps: [
      {
        id: 'start',
        type: 'demo.echo',
        name: 'Start',
        input: { value: 'hello' },
        depends_on: [],
        retries: 0,
        timeout_seconds: 60,
      },
    ],
  }
}

export function WorkflowsPage() {
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [actionError, setActionError] = useState<string | null>(null)

  const { data, error, loading, reload } = useResource<Paginated<WorkflowSummary>>(
    (signal) =>
      api.get<Paginated<WorkflowSummary>>(
        `/api/v1/workflows${query ? `?search=${encodeURIComponent(query)}` : ''}`,
        signal,
      ),
    [query],
  )

  async function createWorkflow() {
    const name = newName.trim()
    if (!name) {
      setActionError('Give the workflow a name first.')
      return
    }
    setCreating(true)
    setActionError(null)
    try {
      await api.post<WorkflowSummary>('/api/v1/workflows', starterDefinition(name))
      setNewName('')
      reload()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The workflow could not be created.')
    } finally {
      setCreating(false)
    }
  }

  async function createDocumentWorkflow() {
    const name = newName.trim() || 'AI document processing'
    setCreating(true)
    setActionError(null)
    try {
      const workflow = await api.post<WorkflowSummary>('/api/v1/workflows', {
        name,
        description: 'Extract text from a PDF, summarize it with Groq, and classify its content.',
        default_max_parallel: 2,
        tags: ['ai', 'document'],
        steps: [
          {
            id: 'extract',
            type: 'document.extract_text',
            name: 'Extract PDF text',
            input: { source_key: 'document_pdf_base64' },
            depends_on: [],
            retries: 0,
            timeout_seconds: 120,
          },
          {
            id: 'summarize',
            type: 'ai.summarize',
            name: 'Summarize document',
            input: { source_step: 'extract' },
            depends_on: ['extract'],
            retries: 2,
            timeout_seconds: 300,
          },
          {
            id: 'classify',
            type: 'ai.classify',
            name: 'Classify document',
            input: { source_step: 'extract' },
            depends_on: ['extract', 'summarize'],
            retries: 2,
            timeout_seconds: 300,
          },
        ],
      })
      await api.post(`/api/v1/workflows/${workflow.id}/publish`, { note: 'Initial AI document workflow' })
      setNewName('')
      navigate(`/workflows/${workflow.id}`)
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The AI document workflow could not be created.')
      reload()
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>Workflows</h1>
          <p className="page-subtitle">Drafts are editable; publishing freezes an immutable version.</p>
        </div>
      </header>

      <section className="panel">
        <div className="toolbar">
          <form
            className="search-form"
            role="search"
            onSubmit={(event) => {
              event.preventDefault()
              setQuery(search.trim())
            }}
          >
            <input
              className="input"
              type="search"
              value={search}
              placeholder="Search workflows"
              aria-label="Search workflows"
              onChange={(event) => setSearch(event.target.value)}
            />
            <button type="submit" className="button button-secondary">
              Search
            </button>
          </form>

          <div className="create-inline">
            <input
              className="input"
              value={newName}
              placeholder="New workflow name"
              aria-label="New workflow name"
              onChange={(event) => setNewName(event.target.value)}
            />
            <button type="button" className="button button-primary" onClick={createWorkflow} disabled={creating}>
              {creating ? 'Creating…' : 'Create workflow'}
            </button>
            <button
              type="button"
              className="button button-secondary"
              onClick={createDocumentWorkflow}
              disabled={creating}
            >
              Create AI document workflow
            </button>
          </div>
        </div>

        {actionError ? (
          <div className="alert alert-error" role="alert">
            {actionError}
          </div>
        ) : null}

        {loading && !data ? (
          <InlineLoader label="Loading workflows…" />
        ) : error ? (
          <ErrorState message={error} onRetry={reload} />
        ) : !data || data.items.length === 0 ? (
          <EmptyState
            title={query ? 'No workflows match that search' : 'No workflows yet'}
            description={
              query
                ? 'Try a different term, or clear the search to see everything.'
                : 'Create your first workflow, then publish it to start a run.'
            }
          />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Version</th>
                <th scope="col">Steps</th>
                <th scope="col">Last run</th>
                <th scope="col">Schedules</th>
                <th scope="col">Updated</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((workflow) => (
                <tr key={workflow.id}>
                  <td>
                    <Link to={`/workflows/${workflow.id}`}>{workflow.name}</Link>
                    {workflow.has_draft_changes ? <span className="pill pill-draft">draft changes</span> : null}
                    {workflow.archived ? <span className="pill">archived</span> : null}
                    {workflow.description ? <p className="row-subtitle">{workflow.description}</p> : null}
                  </td>
                  <td>{workflow.latest_version > 0 ? `v${workflow.latest_version}` : 'not published'}</td>
                  <td>{workflow.step_count}</td>
                  <td>
                    {workflow.last_run_status ? (
                      <>
                        <StatusBadge status={workflow.last_run_status} />
                        <span className="muted"> {formatRelative(workflow.last_run_at)}</span>
                      </>
                    ) : (
                      <span className="muted">never</span>
                    )}
                  </td>
                  <td>{workflow.schedule_count ?? 0}</td>
                  <td>{formatRelative(workflow.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {data && data.total > data.items.length ? (
          <p className="muted">
            Showing {data.items.length} of {data.total} workflows.
          </p>
        ) : null}
      </section>
    </div>
  )
}

export default WorkflowsPage
