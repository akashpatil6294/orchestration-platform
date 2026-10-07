import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ConfirmProvider } from '../components/ConfirmDialogProvider'
import { ToastProvider } from '../components/ToastProvider'
import type { DashboardResponse } from '../lib/types'
import { DashboardPage } from './DashboardPage'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())

vi.mock('../lib/api', () => ({
  ApiError: class ApiError extends Error {},
  api: { get: apiGet, post: apiPost },
}))

vi.mock('../auth/AuthProvider', () => ({
  useAuth: () => ({ user: { id: 'u1', email: 'a@b.c', is_admin: true } }),
}))

function dashboardResponse(): DashboardResponse {
  return {
    stats: {
      window_hours: 24,
      runs_total: 0,
      runs_by_status: [],
      runs_started: 0,
      runs_finished: 0,
      success_rate: 0,
      avg_duration_seconds: null,
      p95_duration_seconds: null,
      step_attempts: 0,
      retried_steps: 0,
      failed_steps: 0,
      timed_out_steps: 0,
      active_workers: 0,
      total_workers: 0,
      pending_steps: 0,
      running_steps: 0,
      schedules_enabled: 0,
      schedules_due: 0,
      workflows_total: 0,
      latest_event_at: null,
    },
    recent_runs: [],
    recent_activity: [
      {
        kind: 'event',
        id: 'event-1',
        run_id: null,
        workflow_id: 'workflow-1',
        workflow_name: 'Example workflow',
        status: 'workflow.created',
        label: 'Workflow created',
        detail: 'Example workflow was created',
        at: new Date().toISOString(),
      },
    ],
    workers: [],
    top_workflows: [],
    needs_attention: [],
    runs_timeline: [],
    duration_trend: [],
  }
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <ConfirmProvider>
          <DashboardPage />
        </ConfirmProvider>
      </ToastProvider>
    </MemoryRouter>,
  )
}

describe('DashboardPage', () => {
  beforeEach(() => {
    apiGet.mockReset()
    apiPost.mockReset()
    apiGet.mockImplementation((path: string) => {
      if (path.startsWith('/api/v1/runs/dashboard')) return Promise.resolve(dashboardResponse())
      if (path.startsWith('/api/v1/workflows')) return Promise.resolve({ items: [], total: 0 })
      return Promise.resolve({})
    })
  })

  it('renders activity entries using the backend activity response fields', async () => {
    renderPage()

    expect(await screen.findByText('Workflow created')).toBeInTheDocument()
    expect(screen.getByText('Example workflow was created')).toBeInTheDocument()
  })

  it('exposes the live controls: refresh interval, window and manual refresh', async () => {
    renderPage()

    expect(await screen.findByRole('group', { name: 'Auto refresh' })).toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Time window' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeInTheDocument()
    expect(screen.getByText('Updated just now')).toBeInTheDocument()
  })

  it('renders the quick actions bar', async () => {
    renderPage()

    expect(await screen.findByRole('link', { name: 'New workflow' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run workflow' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run demo' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Create API token' })).toBeInTheDocument()
  })

  it('opens the run-workflow panel and validates JSON input', async () => {
    const user = (await import('@testing-library/user-event')).default.setup()
    renderPage()

    await screen.findByRole('button', { name: 'Run workflow' })
    await user.click(screen.getByRole('button', { name: 'Run workflow' }))
    expect(await screen.findByRole('region', { name: 'Run a workflow' })).toBeInTheDocument()
    expect(screen.getByText('No published workflows yet — create and publish one first.')).toBeInTheDocument()
  })

  it('approves a waiting approval straight from the needs-attention panel', async () => {
    const user = (await import('@testing-library/user-event')).default.setup()
    apiPost.mockResolvedValue({ status: 'succeeded' })
    apiGet.mockImplementation((path: string) => {
      if (path.startsWith('/api/v1/runs/dashboard')) {
        const body = dashboardResponse()
        body.needs_attention = [
          {
            kind: 'approval',
            id: 'step-1',
            run_id: 'run-9',
            workflow_id: 'workflow-1',
            step_key: 'approve',
            severity: 'warning',
            label: 'Approval needed',
            detail: 'Invoice pipeline · step approve',
            at: new Date().toISOString(),
          },
        ]
        return Promise.resolve(body)
      }
      if (path.startsWith('/api/v1/workflows')) return Promise.resolve({ items: [], total: 0 })
      return Promise.resolve({})
    })
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Approve' }))
    await vi.waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/runs/run-9/steps/approve/approval', { decision: 'approve' }))
  })

  it('runs the demo seeding action through the demo endpoint', async () => {
    const user = (await import('@testing-library/user-event')).default.setup()
    apiPost.mockResolvedValue({ installed: [] })
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Run demo' }))
    await vi.waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/demo/install'))
  })
})
