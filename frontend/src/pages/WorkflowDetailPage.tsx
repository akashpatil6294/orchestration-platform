import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { ApiError, api, uploadDocument } from '../lib/api'
import type { Paginated, RunSummary, ScheduleView, WorkflowDetail, WorkflowSecret, WorkflowVersionDetail } from '../lib/types'
import WorkflowDag from '../components/WorkflowDag'
import { useResource } from '../lib/useResource'
import { useConfirm } from '../components/ConfirmDialogProvider'
import { useToast } from '../components/ToastProvider'
import { ActionButton } from '../components/ActionButton'
import {
  EmptyState,
  ErrorState,
  InlineLoader,
  ProgressBar,
  StatusBadge,
  formatDuration,
  formatRelative,
  formatTimestamp,
} from '../components/StatusView'

const MAX_DOCUMENT_BYTES = 5 * 1024 * 1024

const ROLE_LEVELS: Record<string, number> = { viewer: 1, editor: 2, operator: 3, admin: 4, owner: 5 }

function roleAtLeast(role: string | null | undefined, required: string): boolean {
  if (!role) return false
  return (ROLE_LEVELS[role] ?? 0) >= (ROLE_LEVELS[required] ?? 99)
}

export function WorkflowDetailPage() {
  const { workflowId = '' } = useParams()
  const navigate = useNavigate()
  const toast = useToast()
  const confirmAction = useConfirm()

  const workflow = useResource<WorkflowDetail>(
    (signal) => api.get<WorkflowDetail>(`/api/v1/workflows/${workflowId}`, signal),
    [workflowId],
  )
  const runs = useResource<Paginated<RunSummary>>(
    (signal) => api.get<Paginated<RunSummary>>(`/api/v1/workflows/${workflowId}/runs?limit=10`, signal),
    [workflowId],
  )
  const schedules = useResource<{ items: ScheduleView[]; total: number }>(
    (signal) => api.get<{ items: ScheduleView[]; total: number }>(`/api/v1/schedules?workflow_id=${workflowId}`, signal),
    [workflowId],
  )

  const [note, setNote] = useState('')
  const [runInput, setRunInput] = useState('{}')
  const [documentFile, setDocumentFile] = useState<File | null>(null)
  const [uploadProgress, setUploadProgress] = useState<number | null>(null)
  const [busy, setBusy] = useState<'publish' | 'run' | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [actionNotice, setActionNotice] = useState<string | null>(null)
  const [viewedVersion, setViewedVersion] = useState<WorkflowVersionDetail | null>(null)
  const [secrets, setSecrets] = useState<WorkflowSecret[] | null>(null)
  const [secretName, setSecretName] = useState('')
  const [secretValue, setSecretValue] = useState('')

  const loadSecrets = async () => {
    const list = await api.get<WorkflowSecret[]>(`/api/v1/workflows/${workflowId}/secrets`)
    setSecrets(list)
  }

  const setSecret = async () => {
    const name = secretName.trim()
    if (!name || !secretValue) {
      toast.error('Secret incomplete', 'Provide both a name and a value.')
      return
    }
    if (!/^[A-Za-z0-9_.-]+$/.test(name)) {
      toast.error('Invalid secret name', 'Use letters, digits, dots, dashes or underscores.')
      return
    }
    await api.put(`/api/v1/workflows/${workflowId}/secrets/${encodeURIComponent(name)}`, { name, value: secretValue })
    setSecretValue('')
    setSecretName('')
    toast.success('Secret saved', `“${name}” is encrypted at rest and never displayed again.`)
    loadSecrets().catch(() => undefined)
  }

  const deleteSecret = async (name: string) => {
    const ok = await confirmAction.confirm({
      title: 'Delete secret',
      message: `Delete the secret “${name}”? Runs that reference it will fail until it is set again.`,
      confirmLabel: 'Delete secret',
      danger: true,
    })
    if (!ok) return
    await api.remove(`/api/v1/workflows/${workflowId}/secrets/${encodeURIComponent(name)}`)
    toast.success('Secret deleted', `“${name}” is gone.`)
    loadSecrets().catch(() => undefined)
  }

  const viewVersion = async (version: number) => {
    const detail = await api.get<WorkflowVersionDetail>(`/api/v1/workflows/${workflowId}/versions/${version}`)
    setViewedVersion(detail)
  }

  const toggleArchive = async () => {
    const detail = workflow.data
    if (!detail) return
    const target = !detail.archived
    const ok = await confirmAction.confirm(
      target
        ? {
            title: 'Archive workflow',
            message: `Archive “${detail.name}”? Archived workflows are hidden from the main list and cannot start runs.`,
            confirmLabel: 'Archive',
          }
        : {
            title: 'Unarchive workflow',
            message: `Restore “${detail.name}” to the active workflow list?`,
            confirmLabel: 'Unarchive',
          },
    )
    if (!ok) return
    await api.post(`/api/v1/workflows/${workflowId}/archive?archived=${target}`)
    toast.success(target ? 'Workflow archived' : 'Workflow unarchived')
    workflow.reload()
  }

  const deleteWorkflow = async () => {
    const detail = workflow.data
    if (!detail) return
    const ok = await confirmAction.confirm({
      title: 'Delete workflow',
      message: `Permanently delete “${detail.name}”, its versions and its schedules? Runs already recorded stay in history. This cannot be undone.`,
      confirmLabel: 'Delete workflow',
      danger: true,
    })
    if (!ok) return
    await api.remove(`/api/v1/workflows/${workflowId}`)
    toast.success('Workflow deleted')
    navigate('/workflows')
  }

  async function publish() {
    setBusy('publish')
    setActionError(null)
    setActionNotice(null)
    try {
      const result = await api.post<{ version: number }>(`/api/v1/workflows/${workflowId}/publish`, {
        note: note.trim(),
      })
      setNote('')
      setActionNotice(`Published version ${result.version}.`)
      workflow.reload()
      runs.reload()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The workflow could not be published.')
    } finally {
      setBusy(null)
    }
  }

  async function startRun() {
    let parsed: unknown
    try {
      parsed = runInput.trim() ? JSON.parse(runInput) : {}
    } catch {
      setActionError('Run input must be valid JSON.')
      return
    }
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      setActionError('Run input must be a JSON object.')
      return
    }

    const hasDocumentExtractor = workflow.data?.draft.steps.some((step) => step.type === 'document.extract_text') ?? false
    if (hasDocumentExtractor && !documentFile) {
      setActionError('Choose a PDF document before starting this workflow.')
      return
    }
    if (hasDocumentExtractor && documentFile && documentFile.size > MAX_DOCUMENT_BYTES) {
      setActionError('PDF must be no larger than 5 MB.')
      return
    }

    setBusy('run')
    setActionError(null)
    setActionNotice(null)
    try {
      const input = { ...parsed } as Record<string, unknown>
      if (hasDocumentExtractor && documentFile) {
        // The file streams to /api/v1/documents (the server enforces the 5 MB
        // limit while reading); the run input carries only the document ID so
        // multi-megabyte PDFs never travel inside run inputs.
        setUploadProgress(0)
        const document = await uploadDocument(documentFile, setUploadProgress)
        setUploadProgress(null)
        input.document_id = document.id
      }
      const run = await api.post<RunSummary>(`/api/v1/workflows/${workflowId}/runs`, { input })
      navigate(`/runs/${run.id}`)
    } catch (cause) {
      setActionError(cause instanceof ApiError || cause instanceof Error ? cause.message : 'The run could not be started.')
      setBusy(null)
    }
  }

  const [teams, setTeams] = useState<{ id: string; name: string }[]>([])
  useEffect(() => {
    api
      .get<{ items: { id: string; name: string }[] }>('/api/v1/teams')
      .then((res) => setTeams(res.items))
      .catch(() => setTeams([]))
  }, [])

  if (workflow.loading && !workflow.data) return <InlineLoader label="Loading workflow…" />
  if (workflow.error) return <ErrorState message={workflow.error} onRetry={workflow.reload} />
  if (!workflow.data) return null

  const detail = workflow.data
  const draft = detail.draft
  const published = detail.latest_version > 0
  const userRole = (detail as { user_role?: string | null }).user_role
  const canEdit = roleAtLeast(userRole, 'editor')
  const canDelete = roleAtLeast(userRole, 'admin')
  const editTip = canEdit ? undefined : 'Requires editor role or higher on this workflow’s team' 

  async function assignTeam(teamId: string | null) {
    try {
      await api.patch(`/api/v1/workflows/${workflowId}`, { team_id: teamId })
      workflow.reload()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'Could not change the team.')
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <nav className="breadcrumb" aria-label="Breadcrumb">
            <Link to="/workflows">Workflows</Link>
            <span aria-hidden="true">/</span>
            <span>{detail.name}</span>
          </nav>
          <h1>{detail.name}</h1>
          <p className="page-subtitle">
            {detail.description || 'No description yet.'} ·{' '}
            {published ? `latest published version v${detail.latest_version}` : 'not published'}
            {detail.has_draft_changes ? ' · draft has unpublished changes' : ''}
            {detail.archived ? ' · archived' : ''}
          </p>
        </div>
        <div className="page-actions">
          {canEdit && teams.length > 0 && (
            <label className="team-assign">
              <span className="visually-hidden">Team</span>
              <select
                className="input input-sm"
                value={(detail as { team_id?: string | null }).team_id ?? ''}
                aria-label="Assign to team"
                title="Share this workflow with a team"
                onChange={(event) => assignTeam(event.target.value || null)}
              >
                <option value="">Personal</option>
                {teams.map((team) => (
                  <option key={team.id} value={team.id}>
                    {team.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          <Link className="button button-secondary" to={`/schedules?workflow_id=${detail.id}`}>
            Schedules
          </Link>
          <Link className="button button-primary" to={`/workflows/${detail.id}/edit`}>
            Open builder
          </Link>
          <span title={editTip}>
            <ActionButton
              label={detail.archived ? 'Unarchive' : 'Archive'}
              errorTitle="Archive failed"
              onAction={toggleArchive}
              disabled={!canEdit}
            />
          </span>
          <span title={canDelete ? undefined : 'Requires admin role on this workflow’s team'}>
            <ActionButton label="Delete" variant="danger" errorTitle="Delete failed" onAction={deleteWorkflow} disabled={!canDelete} />
          </span>
        </div>
      </header>

      {actionError ? (
        <div className="alert alert-error" role="alert">
          {actionError}
        </div>
      ) : null}
      {actionNotice ? (
        <div className="alert alert-success" role="status">
          {actionNotice}
        </div>
      ) : null}

      <div className="two-column">
        <section className="panel">
          <h2>Publish a version</h2>
          <p className="muted">
            Publishing validates the graph and freezes an immutable version. Runs always execute a published version.
          </p>
          <label className="field">
            <span>Release note (optional)</span>
            <input
              className="input"
              value={note}
              maxLength={500}
              placeholder="What changed in this version?"
              onChange={(event) => setNote(event.target.value)}
            />
          </label>
          <button type="button" className="button button-primary" onClick={publish} disabled={busy !== null}>
            {busy === 'publish' ? 'Publishing…' : 'Publish draft'}
          </button>

          <h3 className="panel-subheading">Start a run</h3>
          <label className="field">
            <span>Input (JSON)</span>
            <textarea
              className="input textarea"
              rows={4}
              value={runInput}
              spellCheck={false}
              onChange={(event) => setRunInput(event.target.value)}
            />
          </label>
          {detail.draft.steps.some((step) => step.type === 'document.extract_text') ? (
            <label className="field">
              <span>PDF document (up to 5 MB)</span>
              <input
                className="input"
                type="file"
                accept="application/pdf,.pdf"
                onChange={(event) => setDocumentFile(event.target.files?.[0] ?? null)}
              />
              {documentFile ? <small className="muted">{documentFile.name} · {documentFile.size.toLocaleString()} bytes</small> : null}
            </label>
          ) : null}
          {uploadProgress !== null ? (
            <div className="progress" role="progressbar" aria-valuenow={Math.round(uploadProgress * 100)} aria-valuemin={0} aria-valuemax={100}>
              <div className="progress-fill" style={{ width: `${Math.round(uploadProgress * 100)}%` }} />
            </div>
          ) : null}
          <button
            type="button"
            className="button button-primary"
            onClick={startRun}
            disabled={busy !== null || !published}
          >
            {busy === 'run' ? 'Starting…' : 'Start run'}
          </button>
          {!published ? <p className="muted">Publish the workflow before starting a run.</p> : null}
        </section>

        <section className="panel">
          <h2>Version history</h2>
          {detail.versions.length === 0 ? (
            <EmptyState title="No versions yet" description="Publish the draft to create version 1." />
          ) : (
            <ul className="version-list">
              {detail.versions.map((version) => (
                <li key={version.version}>
                  <div className="version-head">
                    <strong>v{version.version}</strong>
                    {version.is_latest ? <span className="pill pill-current">latest</span> : null}
                    <span className="muted">{version.step_count} steps</span>
                  </div>
                  <p className="muted">
                    {formatTimestamp(version.published_at)}
                    {version.published_by ? ` · ${version.published_by}` : ''}
                  </p>
                  {version.publish_note ? <p className="version-note">{version.publish_note}</p> : null}
                  <div className="row-actions" style={{ marginTop: '0.4rem' }}>
                    <button type="button" className="button button-ghost" onClick={() => viewVersion(version.version)}>
                      View definition
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
          {viewedVersion ? (
            <div className="backfill-panel" aria-label={`Version ${viewedVersion.version} definition`}>
              <div className="panel-head">
                <h3>
                  Version {viewedVersion.version} · hash {viewedVersion.definition_hash.slice(0, 12)}
                </h3>
                <button type="button" className="button button-ghost" onClick={() => setViewedVersion(null)}>
                  Close
                </button>
              </div>
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Step</th>
                    <th scope="col">Task type</th>
                    <th scope="col">Depends on</th>
                    <th scope="col">Retries</th>
                    <th scope="col">Timeout</th>
                  </tr>
                </thead>
                <tbody>
                  {viewedVersion.definition.steps.map((step) => (
                    <tr key={step.id}>
                      <td>
                        <strong>{step.name || step.id}</strong>
                        <span className="muted"> ({step.id})</span>
                      </td>
                      <td>
                        <code>{step.type}</code>
                      </td>
                      <td>{step.depends_on.length ? step.depends_on.join(', ') : '—'}</td>
                      <td>{step.retries}</td>
                      <td>{step.timeout_seconds}s</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </section>
      </div>

      <section className="panel" aria-label="Secrets">
        <div className="panel-head">
          <h2>Secrets</h2>
          <button type="button" className="button button-ghost" onClick={() => (secrets === null ? loadSecrets() : setSecrets(null))} aria-expanded={secrets !== null}>
            {secrets === null ? 'Show secrets' : 'Hide secrets'}
          </button>
        </div>
        <p className="muted">
          Values are encrypted at rest, resolved only when a step dispatches, and never displayed again after saving.
        </p>
        {secrets !== null ? (
          <>
            {secrets.length === 0 ? (
              <p className="muted">No secrets stored for this workflow.</p>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">Name</th>
                    <th scope="col">Updated</th>
                    <th scope="col">
                      <span className="visually-hidden">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {secrets.map((secret) => (
                    <tr key={secret.name}>
                      <td>
                        <code>{secret.name}</code>
                      </td>
                      <td>{formatRelative(secret.updated_at)}</td>
                      <td>
                        <span className="row-actions">
                          <ActionButton label="Delete" variant="danger" errorTitle="Delete failed" onAction={() => deleteSecret(secret.name)} />
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <div className="create-inline" style={{ marginTop: '0.8rem' }}>
              <label className="field">
                <span>Name</span>
                <input
                  className="input"
                  value={secretName}
                  placeholder="GROQ_API_KEY"
                  aria-label="Secret name"
                  onChange={(event) => setSecretName(event.target.value)}
                />
              </label>
              <label className="field">
                <span>Value (write-only)</span>
                <input
                  className="input"
                  type="password"
                  autoComplete="off"
                  value={secretValue}
                  aria-label="Secret value"
                  onChange={(event) => setSecretValue(event.target.value)}
                />
              </label>
              <span className="form-actions">
                <ActionButton label="Set secret" variant="primary" pendingLabel="Saving…" errorTitle="Save failed" onAction={setSecret} />
              </span>
            </div>
          </>
        ) : null}
      </section>

      <section className="panel">
        <h2>Workflow graph</h2>
        <WorkflowDag
          steps={(detail.draft.steps ?? []).map((step: { id: string; type: string; depends_on?: string[] }) => ({
            key: step.id,
            type: step.type,
            depends_on: step.depends_on ?? [],
          }))}
        />
      </section>

      <section className="panel">
        <h2>Draft steps</h2>
        <p className="muted">
          {draft.steps.length} step{draft.steps.length === 1 ? '' : 's'} · up to {detail.default_max_parallel} in
          parallel
        </p>
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Step</th>
              <th scope="col">Task type</th>
              <th scope="col">Depends on</th>
              <th scope="col">Requirement</th>
              <th scope="col">Retries</th>
              <th scope="col">Timeout</th>
            </tr>
          </thead>
          <tbody>
            {draft.steps.map((step) => (
              <tr key={step.id}>
                <td>
                  <strong>{step.name || step.id}</strong>
                  <span className="muted"> ({step.id})</span>
                </td>
                <td>
                  <code>{step.type}</code>
                </td>
                <td>{step.depends_on.length ? step.depends_on.join(', ') : '—'}</td>
                <td>{step.required === false ? 'Optional' : 'Required'}</td>
                <td>{step.retries}</td>
                <td>{step.timeout_seconds}s</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <div className="two-column">
        <section className="panel">
          <h2>Recent runs</h2>
          {runs.loading && !runs.data ? (
            <InlineLoader label="Loading runs…" />
          ) : !runs.data || runs.data.items.length === 0 ? (
            <EmptyState title="No runs yet" description="Start a run above to see execution history here." />
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Run</th>
                  <th scope="col">Status</th>
                  <th scope="col">Version</th>
                  <th scope="col">Progress</th>
                  <th scope="col">Duration</th>
                </tr>
              </thead>
              <tbody>
                {runs.data.items.map((run) => (
                  <tr key={run.id}>
                    <td>
                      <Link to={`/runs/${run.id}`}>{formatRelative(run.created_at)}</Link>
                    </td>
                    <td>
                      <StatusBadge status={run.status} />
                    </td>
                    <td>v{run.version}</td>
                    <td className="cell-progress">
                      <ProgressBar value={run.progress} />
                    </td>
                    <td>{formatDuration(run.duration_seconds)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="panel">
          <h2>Schedules</h2>
          {!schedules.data || schedules.data.items.length === 0 ? (
            <EmptyState
              title="No schedules"
              description="Add a schedule to run this workflow on a cron cadence."
              action={
                <Link className="button button-secondary" to={`/schedules?workflow_id=${detail.id}`}>
                  Manage schedules
                </Link>
              }
            />
          ) : (
            <ul className="schedule-list">
              {schedules.data.items.map((schedule) => (
                <li key={schedule.id}>
                  <div className="schedule-head">
                    <strong>{schedule.name}</strong>
                    <span className={`badge badge-${schedule.enabled ? 'good' : 'muted'}`}>
                      {schedule.enabled ? 'Enabled' : 'Paused'}
                    </span>
                  </div>
                  <p className="muted">
                    {schedule.cron_description || schedule.cron_expression} · {schedule.timezone} · v
                    {schedule.effective_version}
                  </p>
                  <p className="muted">Next run {formatTimestamp(schedule.next_run_at)}</p>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  )
}

export default WorkflowDetailPage
