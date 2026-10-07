/** No-dead-buttons test for the settings page, including worker credentials. */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ConfirmProvider } from '../components/ConfirmDialogProvider'
import { ToastProvider } from '../components/ToastProvider'
import { SettingsPage } from './SettingsPage'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiRemove = vi.hoisted(() => vi.fn())

vi.mock('../lib/api', () => ({
  ApiError: class ApiError extends Error {},
  api: { get: apiGet, post: apiPost, patch: vi.fn(), put: vi.fn(), remove: apiRemove },
}))

vi.mock('../auth/AuthProvider', () => ({
  useAuth: () => ({
    user: { id: 'u1', email: 'ops@example.com', display_name: 'Ops', initials: 'O', created_at: '2026-01-01T00:00:00Z' },
    signOut: vi.fn().mockResolvedValue(undefined),
  }),
  displayNameFor: (user: { display_name: string; email: string } | null) => user?.display_name ?? '',
  initialsFor: () => 'O',
  avatarUrlFor: () => null,
}))

const session = {
  user: { id: 'u1', email: 'ops@example.com', display_name: 'Ops', is_admin: true, created_at: '2026-01-01T00:00:00Z', initials: 'O' },
  permissions: [],
  environment: 'development',
  features: {},
  sign_in_method: 'password',
}

const token = {
  id: 'token-1', name: 'CI', token_prefix: 'api_abc', scopes: [],
  created_at: '2026-10-01T00:00:00Z', last_used_at: null, revoked_at: null,
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <ConfirmProvider>
          <SettingsPage />
        </ConfirmProvider>
      </ToastProvider>
    </MemoryRouter>,
  )
}

describe('SettingsPage', () => {
  beforeEach(() => {
    apiGet.mockReset()
    apiPost.mockReset()
    apiRemove.mockReset()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/v1/auth/session') return Promise.resolve(session)
      if (path === '/api/v1/auth/tokens') return Promise.resolve([token])
      return Promise.resolve({})
    })
  })

  it('creates a worker token with the configured id and task types', async () => {
    apiPost.mockResolvedValue({ worker_id: 'etl-1', token: 'wrk-plain', token_prefix: 'wrk_abc', task_types: ['demo.echo'], created_at: '2026-10-05T00:00:00Z' })
    renderPage()
    await screen.findByText('CI')

    fireEvent.change(screen.getByLabelText('Worker id'), { target: { value: 'etl-1' } })
    fireEvent.change(screen.getByLabelText('Worker task types'), { target: { value: 'demo.echo' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create worker token' }))

    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/auth/worker-tokens?worker_id=etl-1&task_types=demo.echo'))
    expect(await screen.findByText(/Copy the token for etl-1 now/)).toBeInTheDocument()
    expect(screen.getByText('wrk-plain')).toBeInTheDocument()
  })

  it('dismisses the once-only worker token banner', async () => {
    apiPost.mockResolvedValue({ worker_id: 'etl-1', token: 'wrk-plain', token_prefix: 'wrk_abc', task_types: [], created_at: '2026-10-05T00:00:00Z' })
    renderPage()
    await screen.findByText('CI')

    fireEvent.change(screen.getByLabelText('Worker id'), { target: { value: 'etl-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create worker token' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText(/Copy the token for etl-1 now/)).not.toBeInTheDocument()
  })

  it('revokes an active API token after confirmation', async () => {
    apiRemove.mockResolvedValue(undefined)
    renderPage()
    await screen.findByText('CI')

    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))
    const dialog = await screen.findByRole('dialog', { name: 'Revoke API token' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Revoke' }))

    await waitFor(() => expect(apiRemove).toHaveBeenCalledWith('/api/v1/auth/tokens/token-1'))
  })
})
