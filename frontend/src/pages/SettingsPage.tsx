import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { avatarUrlFor, displayNameFor, initialsFor, useAuth } from '../auth/AuthProvider'
import { ApiError, api } from '../lib/api'
import type { ApiToken, ApiTokenCreated, SessionInfo, WorkerTokenCreated } from '../lib/types'
import { useResource } from '../lib/useResource'
import { useConfirm } from '../components/ConfirmDialogProvider'
import { useToast } from '../components/ToastProvider'
import { EmptyState, ErrorState, InlineLoader, formatRelative } from '../components/StatusView'

export function SettingsPage() {
  const { user, signOut } = useAuth()
  const navigate = useNavigate()
  const toast = useToast()
  const confirmAction = useConfirm()

  const session = useResource<SessionInfo>((signal) => api.get<SessionInfo>('/api/v1/auth/session', signal), [])
  const tokens = useResource<ApiToken[]>((signal) => api.get<ApiToken[]>('/api/v1/auth/tokens', signal), [])

  const [tokenName, setTokenName] = useState('')
  const [tokenScopes, setTokenScopes] = useState<string[]>(['read'])
  const [creating, setCreating] = useState(false)
  const [created, setCreated] = useState<ApiTokenCreated | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [workerId, setWorkerId] = useState('')
  const [workerTaskTypes, setWorkerTaskTypes] = useState('')
  const [creatingWorkerToken, setCreatingWorkerToken] = useState(false)
  const [createdWorkerToken, setCreatedWorkerToken] = useState<WorkerTokenCreated | null>(null)

  const profile = session.data?.user
  const avatar = avatarUrlFor(user) ?? profile?.avatar_url ?? null
  const name = displayNameFor(user) || profile?.display_name || ''

  async function createToken() {
    const label = tokenName.trim()
    if (!label) {
      setActionError('Give the token a name so you can recognise it later.')
      return
    }
    setCreating(true)
    setActionError(null)
    try {
      const token = await api.post<ApiTokenCreated>('/api/v1/auth/tokens', { name: label, scopes: tokenScopes })
      setCreated(token)
      setTokenName('')
      tokens.reload()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The token could not be created.')
    } finally {
      setCreating(false)
    }
  }

  async function revokeToken(token: ApiToken) {
    const ok = await confirmAction.confirm({
      title: 'Revoke API token',
      message: `Revoke “${token.name}”? Scripts using it stop working immediately.`,
      confirmLabel: 'Revoke',
      danger: true,
    })
    if (!ok) return
    setActionError(null)
    try {
      await api.remove(`/api/v1/auth/tokens/${token.id}`)
      toast.success('Token revoked', `“${token.name}” can no longer call the API.`)
      tokens.reload()
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The token could not be revoked.')
    }
  }

  async function createWorkerToken() {
    const id = workerId.trim()
    if (!id || !/^[A-Za-z0-9_.:-]+$/.test(id)) {
      setActionError('Give the worker an id using letters, digits, dots, dashes, colons or underscores.')
      return
    }
    setCreatingWorkerToken(true)
    setActionError(null)
    try {
      const params = new URLSearchParams({ worker_id: id })
      if (workerTaskTypes.trim()) params.set('task_types', workerTaskTypes.trim())
      const token = await api.post<WorkerTokenCreated>(`/api/v1/auth/worker-tokens?${params.toString()}`)
      setCreatedWorkerToken(token)
      setWorkerId('')
      setWorkerTaskTypes('')
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : 'The worker token could not be created.')
    } finally {
      setCreatingWorkerToken(false)
    }
  }

  async function handleSignOut() {
    await signOut()
    navigate('/login', { replace: true })
  }

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>Settings</h1>
          <p className="page-subtitle">Your account, sign-in method and command-line access.</p>
        </div>
      </header>

      {session.error ? <ErrorState message={session.error} onRetry={session.reload} /> : null}
      {actionError ? (
        <div className="alert alert-error" role="alert">
          {actionError}
        </div>
      ) : null}

      <div className="two-column">
        <section className="panel">
          <h2>Profile</h2>
          {session.loading && !session.data ? (
            <InlineLoader label="Loading your profile…" />
          ) : (
            <>
              <div className="profile-head">
                {avatar ? (
                  <img className="avatar avatar-lg" src={avatar} alt="" referrerPolicy="no-referrer" />
                ) : (
                  <span className="avatar avatar-lg avatar-fallback" aria-hidden="true">
                    {initialsFor(user)}
                  </span>
                )}
                <div>
                  <strong className="profile-name">{name || 'Signed in'}</strong>
                  <p className="muted">{user?.email ?? profile?.email ?? ''}</p>
                </div>
              </div>
              <dl className="metric-list">
                <div>
                  <dt>Sign-in method</dt>
                  <dd>{session.data?.sign_in_method === 'google' ? 'Google' : 'Email and password'}</dd>
                </div>
                <div>
                  <dt>Account created</dt>
                  <dd>{profile ? new Date(profile.created_at).toLocaleDateString() : '—'}</dd>
                </div>
                <div>
                  <dt>Role</dt>
                  <dd>{profile?.is_admin ? 'Administrator' : 'Member'}</dd>
                </div>
                <div>
                  <dt>Environment</dt>
                  <dd>{session.data?.environment ?? '—'}</dd>
                </div>
              </dl>
              <button type="button" className="button button-secondary" onClick={() => void handleSignOut()}>
                Sign out
              </button>
              <p className="muted">
                Signing out ends this browser session. Anything already running on a worker keeps its own lease.
              </p>
            </>
          )}
        </section>

        <section className="panel">
          <h2>API tokens</h2>
          <p className="muted">
            Tokens let scripts and workers call the API without your browser session. A token is shown once and never
            again.
          </p>

          <div className="create-inline">
            <input
              className="input"
              value={tokenName}
              placeholder="Token name"
              aria-label="Token name"
              onChange={(event) => setTokenName(event.target.value)}
            />
            <button type="button" className="button button-primary" onClick={createToken} disabled={creating}>
              {creating ? 'Creating…' : 'Create token'}
            </button>
          </div>
          <div className="scope-selector" role="group" aria-label="Token scopes">
            {(['read', 'run', 'manage'] as const).map((scope) => (
              <label key={scope} className="scope-option" title={
                scope === 'read' ? 'Read workflows, runs and results' :
                scope === 'run' ? 'Read plus start, pause, cancel and retry runs' :
                'Full access: manage workflows, teams and tokens'
              }>
                <input
                  type="checkbox"
                  checked={tokenScopes.includes(scope)}
                  onChange={(event) => {
                    setTokenScopes((prev) =>
                      event.target.checked ? [...prev, scope] : prev.filter((s) => s !== scope),
                    )
                  }}
                />
                {scope}
              </label>
            ))}
          </div>

          {created ? (
            <div className="alert alert-success" role="status">
              <strong>Copy this token now — it will not be shown again.</strong>
              <code className="token-value">{created.token}</code>
            </div>
          ) : null}

          {tokens.loading && !tokens.data ? (
            <InlineLoader label="Loading tokens…" />
          ) : tokens.error ? (
            <ErrorState message={tokens.error} onRetry={tokens.reload} />
          ) : !tokens.data || tokens.data.length === 0 ? (
            <EmptyState title="No API tokens" description="Create one to automate this account from a script." />
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Prefix</th>
                  <th scope="col">Last used</th>
                  <th scope="col">Status</th>
                  <th scope="col" />
                </tr>
              </thead>
              <tbody>
                {tokens.data.map((token) => (
                  <tr key={token.id}>
                    <td>{token.name}</td>
                    <td>
                      <code>{token.token_prefix}…</code>
                    </td>
                    <td>{formatRelative(token.last_used_at)}</td>
                    <td>{token.revoked_at ? 'Revoked' : 'Active'}</td>
                    <td>
                      {token.revoked_at ? null : (
                        <button
                          type="button"
                          className="button button-ghost button-danger"
                          onClick={() => void revokeToken(token)}
                        >
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="panel">
          <h2>Worker credentials</h2>
          <p className="muted">
            A worker token lets a worker process register, claim steps and report results. It is shown once and never
            again. Restrict the task types to keep the worker's allow-list explicit.
          </p>

          <div className="create-inline">
            <label className="field">
              <span>Worker id</span>
              <input
                className="input"
                value={workerId}
                placeholder="etl-worker-1"
                aria-label="Worker id"
                onChange={(event) => setWorkerId(event.target.value)}
              />
            </label>
            <label className="field">
              <span>Task types (comma-separated, optional)</span>
              <input
                className="input"
                value={workerTaskTypes}
                placeholder="demo.echo, http.request"
                aria-label="Worker task types"
                onChange={(event) => setWorkerTaskTypes(event.target.value)}
              />
            </label>
            <button type="button" className="button button-primary" onClick={createWorkerToken} disabled={creatingWorkerToken}>
              {creatingWorkerToken ? 'Creating…' : 'Create worker token'}
            </button>
          </div>

          {createdWorkerToken ? (
            <div className="alert alert-success" role="status">
              <strong>
                Copy the token for {createdWorkerToken.worker_id} now — it will not be shown again.
              </strong>
              <code className="token-value">{createdWorkerToken.token}</code>
              <button type="button" className="button button-ghost" onClick={() => setCreatedWorkerToken(null)}>
                Dismiss
              </button>
            </div>
          ) : null}
          <p className="muted">
            After creating a credential, start the worker with{' '}
            <code>WORKER_TOKEN=… WORKER_ID=… python -m app.sample_worker</code> and watch it appear under Workers &amp;
            queues.
          </p>
        </section>
      </div>
    </div>
  )
}

export default SettingsPage
