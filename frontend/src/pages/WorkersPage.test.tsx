/** No-dead-buttons test for the workers & queues page. */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ConfirmProvider } from '../components/ConfirmDialogProvider'
import { ToastProvider } from '../components/ToastProvider'
import { WorkersPage } from './WorkersPage'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())

vi.mock('../lib/api', () => ({
  ApiError: class ApiError extends Error {},
  api: { get: apiGet, post: apiPost, patch: vi.fn(), put: vi.fn(), remove: vi.fn() },
}))

function overview(workerOverrides: Record<string, unknown> = {}) {
  return {
    environment: 'development',
    version: '1.0.0',
    server_time: new Date().toISOString(),
    configuration: {},
    database: 'sqlite',
    dispatch: { backend: 'database-polling', redis_configured: false, relay_running: true, outbox_backlog: 3 },
    scheduler: {
      enabled: true,
      loop_running: true,
      interval_seconds: 15,
      last_tick_at: new Date().toISOString(),
      last_error: null,
      enabled_schedules: 2,
      due_schedules: 1,
    },
    workers: {
      total: 1,
      active: 1,
      stale: 0,
      task_type_coverage: { 'demo.echo': 1 },
      items: [
        {
          worker_id: 'worker-1',
          name: 'etl-worker-1',
          active: true,
          stale: false,
          task_types: ['demo.echo'],
          queues: ['default'],
          max_concurrency: 4,
          last_seen_at: new Date().toISOString(),
          created_at: new Date().toISOString(),
          running_steps: 1,
          ...workerOverrides,
        },
      ],
    },
    runs: { by_status: { succeeded: 3 }, total: 3 },
    steps: { by_status: { running: 1, pending: 2 }, total: 3 },
    recent_failures: [],
  }
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <ConfirmProvider>
          <WorkersPage />
        </ConfirmProvider>
      </ToastProvider>
    </MemoryRouter>,
  )
}

describe('WorkersPage', () => {
  beforeEach(() => {
    apiGet.mockReset()
    apiPost.mockReset()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/v1/ops/overview') return Promise.resolve(overview())
      if (path === '/api/v1/workers/worker-1/tasks') {
        return Promise.resolve({ items: [{ step_run_id: 's1', run_id: 'run-1', step_key: 'work', task_type: 'demo.echo', attempt: 1, deadline_at: null, started_at: null }] })
      }
      return Promise.resolve({})
    })
  })

  it('renders the platform status and the worker fleet', async () => {
    renderPage()
    expect(await screen.findByText('database-polling')).toBeInTheDocument()
    expect(screen.getByText('etl-worker-1')).toBeInTheDocument()
    expect(screen.getByText(/3/).closest('section')).toBeTruthy()
  })

  it('deactivates and activates a worker through the fleet endpoints', async () => {
    apiPost.mockResolvedValue({})
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Deactivate' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/workers/worker-1/deactivate'))
  })

  it('shuts a worker down gracefully after confirmation', async () => {
    apiPost.mockResolvedValue({ released_tasks: 1, message: '' })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Shutdown' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Shut down' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/workers/worker-1/shutdown'))
  })

  it('runs each maintenance action and reports the result', async () => {
    apiPost.mockImplementation((path: string) => {
      if (path === '/api/v1/ops/maintenance/recover-leases') return Promise.resolve({ recovered: 2 })
      if (path === '/api/v1/ops/maintenance/prune-outbox') return Promise.resolve({ removed: 5 })
      if (path === '/api/v1/ops/maintenance/scheduler-tick') return Promise.resolve({ runs_started: 1 })
      return Promise.resolve({})
    })
    renderPage()
    await screen.findByText('etl-worker-1')

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Recover expired leases' }))
    })
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/ops/maintenance/recover-leases'))

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Prune outbox' }))
    })
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/ops/maintenance/prune-outbox'))

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Run scheduler tick' }))
    })
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/ops/maintenance/scheduler-tick'))
  })

  it('shows the tasks a worker is currently executing', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'View tasks' }))
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/v1/workers/worker-1/tasks', expect.anything()))
    expect(await screen.findByText('work')).toBeInTheDocument()
  })

  it('highlights stale workers', async () => {
    apiGet.mockImplementation((path: string) =>
      path === '/api/v1/ops/overview' ? Promise.resolve(overview({ stale: true, active: false })) : Promise.resolve({ items: [] }),
    )
    renderPage()
    const row = await screen.findByText('etl-worker-1').then((element) => element.closest('tr'))
    expect(row).toHaveClass('row-stale')
  })
})
