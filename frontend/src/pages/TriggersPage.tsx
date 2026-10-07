import { useState } from 'react'

import { ApiError, api } from '../lib/api'
import type { Paginated, WorkflowSummary, WorkflowTrigger } from '../lib/types'
import { EmptyState, ErrorState, InlineLoader, formatRelative } from '../components/StatusView'
import { useConfirm } from '../components/ConfirmDialogProvider'
import { useToast } from '../components/ToastProvider'
import { useResource } from '../lib/useResource'

interface TriggerList {
  items: WorkflowTrigger[]
  total: number
}

function generateSigningSecret(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32))
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
}

export function TriggersPage() {
  const confirmAction = useConfirm()
  const toast = useToast()
  const triggers = useResource<TriggerList>((signal) => api.get<TriggerList>('/api/v1/triggers', signal), [])
  const workflows = useResource<Paginated<WorkflowSummary>>(
    (signal) => api.get<Paginated<WorkflowSummary>>('/api/v1/workflows', signal),
    [],
  )
  const published = (workflows.data?.items ?? []).filter((workflow) => workflow.latest_version > 0 && !workflow.archived)

  const [workflowId, setWorkflowId] = useState('')
  const [kind, setKind] = useState<'webhook' | 'workflow_success'>('webhook')
  const [sourceWorkflowId, setSourceWorkflowId] = useState('')
  const [name, setName] = useState('')
  const [rateLimit, setRateLimit] = useState('60')
  const [mappingText, setMappingText] = useState('{}')
  const [busy, setBusy] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [secretNotice, setSecretNotice] = useState<{ endpoint: string; secret: string } | null>(null)

  async function createTrigger() {
    if (!workflowId) {
      setActionError('Choose a published target workflow.')
      return
    }
    let inputMapping: Record<string, string>
    try {
      const parsed: unknown = JSON.parse(mappingText)
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('Expected a JSON object.')
      inputMapping = parsed as Record<string, string>
      if (Object.values(inputMapping).some((value) => typeof value !== 'string')) {
        throw new Error('Every mapping value must be a JSON path string.')
      }
    } catch (cause) {
      setActionError(cause instanceof Error ? `Input mapping: ${cause.message}` : 'Input mapping must be valid JSON.')
      return
    }
    setBusy('create')
    setActionError(null)
    setSecretNotice(null)
    try {
      const signingSecret = kind === 'webhook' ? generateSigningSecret() : undefined
      const trigger = await api.post<WorkflowTrigger>(`/api/v1/workflows/${workflowId}/triggers`, {
        name: name.trim() || (kind === 'webhook' ? 'Webhook trigger' : 'Workflow success trigger'),
        kind,
        ...(kind === 'workflow_success' ? { source_workflow_id: sourceWorkflowId } : {}),
        ...(signingSecret ? { signing_secret: signingSecret } : {}),
        ...(kind === 'webhook' ? { rate_limit_per_minute: Number(rateLimit) } : {}),
        input_mapping: inputMapping,
      })
      if (signingSecret) setSecretNotice({ endpoint: trigger.endpoint ?? '', secret: signingSecret })
      setName('')
      triggers.reload()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The trigger could not be created.')
    } finally {
      setBusy(null)
    }
  }

  async function updateTrigger(trigger: WorkflowTrigger, action: 'toggle' | 'rotate' | 'delete') {
    setBusy(trigger.id)
    setActionError(null)
    if (action === 'rotate') setSecretNotice(null)
    try {
      if (action === 'toggle') {
        await api.patch(`/api/v1/triggers/${trigger.id}`, { enabled: !trigger.enabled })
      } else if (action === 'rotate') {
        const signingSecret = generateSigningSecret()
        const updated = await api.post<WorkflowTrigger>(`/api/v1/triggers/${trigger.id}/rotate-secret`, { signing_secret: signingSecret })
        setSecretNotice({ endpoint: updated.endpoint ?? '', secret: signingSecret })
      } else {
        const ok = await confirmAction.confirm({
          title: 'Delete trigger',
          message: `Delete the ${trigger.kind === 'webhook' ? 'webhook' : 'workflow-success'} trigger for “${trigger.workflow_name}”? Calls to its endpoint will stop creating runs.`,
          confirmLabel: 'Delete trigger',
          danger: true,
        })
        if (!ok) {
          setBusy(null)
          return
        }
        await api.remove(`/api/v1/triggers/${trigger.id}`)
        toast.success('Trigger deleted')
      }
      triggers.reload()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The trigger could not be updated.')
    } finally {
      setBusy(null)
    }
  }

  const sourceOptions = published.filter((workflow) => workflow.id !== workflowId)

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>Triggers</h1>
          <p className="page-subtitle">Start published workflows from signed webhooks or successful upstream runs.</p>
        </div>
      </header>

      {actionError ? <div className="alert alert-error" role="alert">{actionError}</div> : null}
      {secretNotice ? (
        <section className="alert alert-success" role="status" aria-label="Signing secret shown once">
          <strong>Copy this signing secret now. It is shown only once.</strong>
          <p>Endpoint: <code>{secretNotice.endpoint}</code></p>
          <label className="field">
            <span>Signing secret</span>
            <input className="input" readOnly value={secretNotice.secret} aria-label="Signing secret" />
          </label>
          <p className="muted">Sign the exact raw request body as <code>timestamp.body</code> with HMAC-SHA256. Send the timestamp, signature, and an Idempotency-Key header.</p>
          <button type="button" className="button button-secondary" onClick={() => setSecretNotice(null)}>Dismiss secret</button>
        </section>
      ) : null}

      <section className="panel">
        <h2>Add a trigger</h2>
        {workflows.loading && !workflows.data ? <InlineLoader label="Loading workflows…" /> : published.length === 0 ? (
          <EmptyState title="No published workflows" description="Publish a workflow before connecting a trigger." />
        ) : (
          <div className="form-grid">
            <label className="field">
              <span>Trigger type</span>
              <select className="input" value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}>
                <option value="webhook">Signed webhook</option>
                <option value="workflow_success">Workflow succeeds</option>
              </select>
            </label>
            <label className="field">
              <span>Target workflow</span>
              <select className="input" value={workflowId} onChange={(event) => setWorkflowId(event.target.value)}>
                <option value="">Select a workflow</option>
                {published.map((workflow) => <option key={workflow.id} value={workflow.id}>{workflow.name} (v{workflow.latest_version})</option>)}
              </select>
            </label>
            {kind === 'workflow_success' ? (
              <label className="field">
                <span>Start when this workflow succeeds</span>
                <select className="input" value={sourceWorkflowId} onChange={(event) => setSourceWorkflowId(event.target.value)}>
                  <option value="">Select a source workflow</option>
                  {sourceOptions.map((workflow) => <option key={workflow.id} value={workflow.id}>{workflow.name} (v{workflow.latest_version})</option>)}
                </select>
              </label>
            ) : null}
            {kind === 'webhook' ? (
              <label className="field">
                <span>Requests allowed per minute</span>
                <input className="input" type="number" min="1" max="10000" value={rateLimit} onChange={(event) => setRateLimit(event.target.value)} />
              </label>
            ) : null}
            <label className="field">
              <span>Name (optional)</span>
              <input className="input" value={name} onChange={(event) => setName(event.target.value)} />
            </label>
            <label className="field field-wide">
              <span>Input mapping (JSON object)</span>
              <textarea className="input" rows={4} value={mappingText} onChange={(event) => setMappingText(event.target.value)} aria-label="Input mapping" />
              <small>Map workflow input names to payload paths, for example <code>{'{"order_id":"payload.data.id"}'}</code>. Use <code>{'{}'}</code> to pass the full webhook payload.</small>
            </label>
            <div className="form-actions">
              <button type="button" className="button button-primary" onClick={() => void createTrigger()} disabled={busy === 'create'}>
                {busy === 'create' ? 'Creating…' : 'Create trigger'}
              </button>
            </div>
          </div>
        )}
      </section>

      <section className="panel">
        <h2>Existing triggers</h2>
        {triggers.loading && !triggers.data ? <InlineLoader label="Loading triggers…" /> : triggers.error ? (
          <ErrorState message={triggers.error} onRetry={triggers.reload} />
        ) : !triggers.data || triggers.data.items.length === 0 ? (
          <EmptyState title="No triggers yet" description="Add a trigger to start a workflow from an external event or another workflow." />
        ) : (
          <table className="table">
            <thead><tr><th scope="col">Trigger</th><th scope="col">Target</th><th scope="col">Source / endpoint</th><th scope="col">Input mapping</th><th scope="col">Actions</th></tr></thead>
            <tbody>
              {triggers.data.items.map((trigger) => (
                <tr key={trigger.id}>
                  <td><strong>{trigger.name}</strong><p className="row-subtitle"><span className={`pill ${trigger.enabled ? 'pill-current' : ''}`}>{trigger.enabled ? 'enabled' : 'paused'}</span><span className="muted"> {trigger.kind.replace('_', ' ')} · {formatRelative(trigger.created_at)}</span></p></td>
                  <td>{trigger.workflow_name}</td>
                  <td>{trigger.kind === 'webhook' ? <code>{trigger.endpoint}</code> : (workflows.data?.items.find((workflow) => workflow.id === trigger.source_workflow_id)?.name ?? trigger.source_workflow_id)}</td>
                  <td><code>{JSON.stringify(trigger.input_mapping)}</code></td>
                  <td><div className="row-actions">
                    <button type="button" className="button button-ghost" onClick={() => void updateTrigger(trigger, 'toggle')} disabled={busy === trigger.id}>{trigger.enabled ? 'Pause' : 'Enable'}</button>
                    {trigger.kind === 'webhook' ? <button type="button" className="button button-ghost" onClick={() => void updateTrigger(trigger, 'rotate')} disabled={busy === trigger.id}>Rotate secret</button> : null}
                    <button type="button" className="button button-ghost button-danger" onClick={() => void updateTrigger(trigger, 'delete')} disabled={busy === trigger.id}>Delete</button>
                  </div></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}

export default TriggersPage
