/** No-dead-buttons test for the run detail page. */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ConfirmProvider } from '../components/ConfirmDialogProvider'
import { ToastProvider } from '../components/ToastProvider'
import { RunDetailPage } from './RunDetailPage'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiRemove = vi.hoisted(() => vi.fn())

vi.mock('../lib/api', () => ({
  ApiError: class ApiError extends Error {},
  api: { get: apiGet, post: apiPost, patch: vi.fn(), put: vi.fn(), remove: apiRemove },
}))

vi.mock('../auth/AuthProvider', () => ({
  useAuth: () => ({ user: { id: 'u1', email: 'a@b.c', is_admin: true } }),
}))

function runDetail(overrides: Record<string, unknown> = {}) {
  return {
    id: 'run-1',
    workflow_id: 'workflow-1',
    workflow_name: 'Nightly job',
    version: 2,
    status: 'failed',
    trigger: 'schedule',
    created_at: '2026-10-05T10:00:00Z',
    started_at: '2026-10-05T10:00:01Z',
    finished_at: '2026-10-05T10:01:00Z',
    duration_seconds: 59,
    step_counts: { failed: 1, succeeded: 1 },
    total_steps: 2,
    completed_steps: 1,
    progress: 0.5,
    cancel_requested: false,
    max_parallel: 4,
    input: {},
    output: null,
    error: null,
    retryable_steps: ['extract'],
    latest_event_seq: 0,
    steps: [
      {
        id: 'step-1', key: 'extract', name: 'Extract', type: 'demo.echo', status: 'failed', depends_on: [],
        attempts: 2, retry_limit: 1, required: true, timeout_seconds: 60, available_at: null, deadline_at: null,
        started_at: '2026-10-05T10:00:01Z', finished_at: '2026-10-05T10:00:30Z', duration_seconds: 29,
        worker_id: 'worker-1', output: null, error: { code: 'runtime_error', message: 'boom' } as { code: string; message: string } | null, input: {},
        log_lines: 0, last_log_at: null, downstream: [], retry_at: null, spec: {}, parent_step_id: null,
        child_run_id: null, foreach_index: null, queue: 'default', wait_reason: null as string | null,
      },
    ],
    ...overrides,
  }
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/runs/run-1']}>
      <ToastProvider>
        <ConfirmProvider>
          <Routes>
            <Route path="/runs/:runId" element={<RunDetailPage />} />
            <Route path="/workflows/:workflowId" element={<div>back to workflow</div>} />
          </Routes>
        </ConfirmProvider>
      </ToastProvider>
    </MemoryRouter>,
  )
}

describe('RunDetailPage', () => {
  beforeEach(() => {
    apiGet.mockReset()
    apiPost.mockReset()
    apiRemove.mockReset()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/v1/runs/run-1') return Promise.resolve(runDetail())
      if (path === '/api/v1/runs/run-1/events?limit=50') return Promise.resolve({ items: [], latest_seq: 0 })
      if (path === '/api/v1/runs/run-1/steps/step-1/attempts') {
        return Promise.resolve({
          step_run_id: 'step-1',
          step_key: 'extract',
          items: [
            { id: 'attempt-1', attempt: 1, worker_id: 'worker-1', status: 'failed', started_at: null, finished_at: null, duration_seconds: 12, output: null, error: null, log_lines: 0 },
          ],
        })
      }
      return Promise.resolve({})
    })
    apiPost.mockResolvedValue({ retried_steps: ['extract'] })
  })

  it('cancels a live run only after confirmation', async () => {
    apiGet.mockImplementation((path: string) =>
      path === '/api/v1/runs/run-1'
        ? Promise.resolve(runDetail({ status: 'running', step_counts: { running: 1 }, completed_steps: 0, progress: 0, retryable_steps: [] }))
        : Promise.resolve({ items: [], latest_seq: 0 }),
    )
    renderPage()
    await screen.findByText('Extract')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel run' }))

    // The dialog appears first; no request is sent until it is confirmed.
    const dialog = await screen.findByRole('dialog', { name: 'Cancel run' })
    expect(apiPost).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel run' }))

    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/runs/run-1/cancel'))
  })

  it('retries failed steps after confirmation', async () => {
    renderPage()
    await screen.findByText('Extract')
    fireEvent.click(screen.getByRole('button', { name: 'Retry failed steps' }))
    const retryDialog = await screen.findByRole('dialog', { name: 'Retry failed steps' })
    fireEvent.click(within(retryDialog).getByRole('button', { name: 'Retry' }))

    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/runs/run-1/retry', { steps: [], reset_downstream: true }))
  })

  it('re-runs from a failed step after confirmation', async () => {
    renderPage()
    await screen.findByText('Extract')
    fireEvent.click(screen.getByRole('button', { name: 'Re-run from here' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Re-run' }))

    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/runs/run-1/rerun-from-step', { steps: ['extract'], reset_downstream: true }))
  })

  it('loads the attempt history for a step', async () => {
    renderPage()
    await screen.findByText('Extract')
    fireEvent.click(screen.getByRole('button', { name: 'View attempts (2)' }))
    expect(await screen.findByRole('table', { name: 'Attempt history for extract' })).toBeInTheDocument()
  })

  it('shows aggregated AI usage when the stats report it', async () => {
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/v1/runs/run-1') return Promise.resolve(runDetail())
      if (path === '/api/v1/runs/run-1/events?limit=50') return Promise.resolve({ items: [], latest_seq: 0 })
      if (path === '/api/v1/runs/run-1/stats')
        return Promise.resolve({
          run_id: 'run-1',
          status: 'failed',
          step_counts: { failed: 1 },
          total_attempts: 2,
          retried_steps: 1,
          ai_usage: {
            steps: 2,
            models: [
              { model: 'test-model', prompt_tokens: 140, completion_tokens: 60, total_tokens: 200, cost_usd: 0.00026, steps: 2 },
            ],
          },
        })
      return Promise.resolve({})
    })
    renderPage()
    expect(await screen.findByText('AI usage')).toBeInTheDocument()
    expect(screen.getByText('test-model')).toBeInTheDocument()
    expect(screen.getByText('$0.000260')).toBeInTheDocument()
  })

  it('hides the AI usage panel when the run used no AI steps', async () => {
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/v1/runs/run-1') return Promise.resolve(runDetail())
      if (path === '/api/v1/runs/run-1/events?limit=50') return Promise.resolve({ items: [], latest_seq: 0 })
      if (path === '/api/v1/runs/run-1/stats')
        return Promise.resolve({ run_id: 'run-1', status: 'failed', step_counts: {}, total_attempts: 0, retried_steps: 0, ai_usage: null })
      return Promise.resolve({})
    })
    renderPage()
    await screen.findByText('Extract')
    expect(screen.queryByText('AI usage')).not.toBeInTheDocument()
  })

  it('shows the per-step wait reason', async () => {
    const detail = runDetail()
    detail.steps = [
      {
        ...detail.steps[0],
        status: 'queued',
        wait_reason: 'waiting_for_worker',
        queue: 'default',
        error: null,
      },
    ]
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/v1/runs/run-1') return Promise.resolve(detail)
      if (path === '/api/v1/ops/capacity')
        return Promise.resolve({
          active_workers: 0,
          queues: { default: { queued_steps: 1, oldest_queued_age_seconds: 120, task_types: ['demo.echo'] } },
          task_types: { 'demo.echo': { covered: false, queues: ['default'] } },
          embedded_worker_allowed: false,
          queue_wait_warning_seconds: 30,
        })
      return Promise.resolve({ items: [], latest_seq: 0 })
    })
    renderPage()
    await screen.findByText('Extract')
    expect(screen.getByText('Waiting for a worker')).toBeInTheDocument()
    expect(await screen.findByRole('alert')).toHaveTextContent('No worker is available')
  })

  it('deletes a finished run after confirmation and navigates to the workflow', async () => {
    apiRemove.mockResolvedValue(undefined)
    renderPage()
    await screen.findByText('Extract')
    fireEvent.click(screen.getByRole('button', { name: 'Delete run' }))
    const deleteDialog = await screen.findByRole('dialog', { name: 'Delete run' })
    fireEvent.click(within(deleteDialog).getByRole('button', { name: 'Delete run' }))

    await waitFor(() => expect(apiRemove).toHaveBeenCalledWith('/api/v1/runs/run-1'))
    expect(await screen.findByText('back to workflow')).toBeInTheDocument()
  })
})
