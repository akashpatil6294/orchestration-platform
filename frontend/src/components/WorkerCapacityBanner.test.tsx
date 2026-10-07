import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ToastProvider } from '../components/ToastProvider'
import { WorkerCapacityBanner } from '../components/WorkerCapacityBanner'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const mockUser = vi.hoisted(() => ({ is_admin: true }))

vi.mock('../lib/api', () => ({
  ApiError: class ApiError extends Error {},
  api: { get: apiGet, post: apiPost },
}))

vi.mock('../auth/AuthProvider', () => ({
  useAuth: () => ({ user: { id: 'u1', email: 'a@b.c', is_admin: mockUser.is_admin } }),
}))

function capacityResponse(overrides = {}) {
  return {
    active_workers: 0,
    queues: {
      default: { queued_steps: 2, oldest_queued_age_seconds: 120, task_types: ['demo.echo'] },
    },
    task_types: { 'demo.echo': { covered: false, queues: ['default'] } },
    embedded_worker_allowed: true,
    queue_wait_warning_seconds: 30,
    ...overrides,
  }
}

function renderBanner() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <WorkerCapacityBanner />
      </ToastProvider>
    </MemoryRouter>,
  )
}

function setCapacity(response: object) {
  apiGet.mockImplementation((path: string) => {
    if (path === '/api/v1/auth/session') {
      return Promise.resolve({ user: { is_admin: mockUser.is_admin } })
    }
    return Promise.resolve(response)
  })
}

describe('WorkerCapacityBanner', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockUser.is_admin = true
    setCapacity(capacityResponse())
  })

  it('shows the banner when steps wait with no covering worker', async () => {
    renderBanner()
    expect(await screen.findByRole('alert')).toHaveTextContent('No worker is available')
    expect(screen.getByText('demo.echo')).toBeInTheDocument()
  })

  it('stays hidden when a worker covers the queue', async () => {
    setCapacity(
      capacityResponse({
        active_workers: 1,
        task_types: { 'demo.echo': { covered: true, queues: ['default'] } },
      }),
    )
    renderBanner()
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('start button calls the endpoint and is admin-gated', async () => {
    apiPost.mockResolvedValue({ worker_id: 'embedded-x', status: 'started' })
    const user = userEvent.setup()
    renderBanner()
    const button = await screen.findByRole('button', { name: 'Start embedded worker' })
    await user.click(button)
    expect(apiPost).toHaveBeenCalledWith('/api/v1/ops/embedded-worker/start', {})
  })

  it('hides the start button for non-admins', async () => {
    mockUser.is_admin = false
    renderBanner()
    await screen.findByRole('alert')
    expect(screen.queryByRole('button', { name: 'Start embedded worker' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy worker command' })).toBeInTheDocument()
  })

  it('copy button writes the worker command to the clipboard', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.spyOn(navigator.clipboard, 'writeText').mockImplementation(writeText)
    const user = userEvent.setup()
    renderBanner()
    await user.click(await screen.findByRole('button', { name: 'Copy worker command' }))
    expect(writeText).toHaveBeenCalledOnce()
    expect(writeText.mock.calls[0][0]).toContain('app.sample_worker')
  })

  it('generate token mints a credential and prefills the copied command', async () => {
    apiPost.mockResolvedValue({ worker_id: 'worker-abc', token: 'wrk_secret123' })
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.spyOn(navigator.clipboard, 'writeText').mockImplementation(writeText)
    const user = userEvent.setup()
    renderBanner()
    await user.click(await screen.findByRole('button', { name: 'Generate worker token' }))
    expect(apiPost).toHaveBeenCalledWith('/api/v1/workers', { worker_id: expect.any(String), queues: ['default'] })
    // The one-time token panel appears…
    expect(await screen.findByText(/shown once/)).toBeInTheDocument()
    expect(screen.getByText('wrk_secret123')).toBeInTheDocument()
    // …and the copy button now prefills the minted token.
    await user.click(screen.getByRole('button', { name: 'Copy start command (token prefilled)' }))
    expect(writeText).toHaveBeenCalledOnce()
    expect(writeText.mock.calls[0][0]).toContain('WORKER_TOKEN=wrk_secret123')
    expect(writeText.mock.calls[0][0]).toContain('WORKER_ID=worker-abc')
  })

  it('hides the generate-token button for non-admins', async () => {
    mockUser.is_admin = false
    renderBanner()
    await screen.findByRole('alert')
    expect(screen.queryByRole('button', { name: 'Generate worker token' })).not.toBeInTheDocument()
  })
})
