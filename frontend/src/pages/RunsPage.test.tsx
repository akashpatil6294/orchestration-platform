/**
 * No-dead-buttons test for the runs page: every control either sends the
 * expected request (method + URL + body) or updates the UI.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ConfirmProvider } from '../components/ConfirmDialogProvider'
import { ToastProvider } from '../components/ToastProvider'
import { RunsPage } from './RunsPage'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiRemove = vi.hoisted(() => vi.fn())

vi.mock('../lib/api', () => ({
  ApiError: class ApiError extends Error {},
  api: { get: apiGet, post: apiPost, patch: vi.fn(), put: vi.fn(), remove: apiRemove },
}))

function run(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    workflow_id: 'workflow-1',
    workflow_name: 'Nightly job',
    version: 3,
    status: 'succeeded',
    trigger: 'schedule',
    created_at: '2026-10-05T10:00:00Z',
    started_at: '2026-10-05T10:00:01Z',
    finished_at: '2026-10-05T10:01:00Z',
    duration_seconds: 59,
    step_counts: { succeeded: 2 },
    total_steps: 2,
    completed_steps: 2,
    progress: 1,
    cancel_requested: false,
    ...overrides,
  }
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/runs']}>
      <ToastProvider>
        <ConfirmProvider>
          <RunsPage />
        </ConfirmProvider>
      </ToastProvider>
    </MemoryRouter>,
  )
}

describe('RunsPage', () => {
  beforeEach(() => {
    apiGet.mockReset()
    apiPost.mockReset()
    apiRemove.mockReset()
    apiGet.mockImplementation((path: string) => {
      if (path.startsWith('/api/v1/runs')) {
        return Promise.resolve({ items: [run('run-1'), run('run-2', { id: 'run-2', status: 'failed', step_counts: { failed: 1 }, completed_steps: 1, progress: 0.5 })], total: 2, limit: 25, offset: 0, has_more: false })
      }
      if (path.startsWith('/api/v1/workflows')) return Promise.resolve({ items: [], total: 0 })
      return Promise.resolve({})
    })
  })

  it('sends filters and sort to the runs endpoint', async () => {
    renderPage()
    await screen.findAllByText('Nightly job')

    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'failed' } })
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith(expect.stringContaining('status=failed'), expect.anything()))
    fireEvent.change(screen.getByLabelText('Sort'), { target: { value: 'longest' } })
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith(expect.stringContaining('sort=longest'), expect.anything()))
  })

  it('sends the search query when the search form is submitted', async () => {
    renderPage()
    await screen.findAllByText('Nightly job')
    fireEvent.change(screen.getByLabelText('Search runs'), { target: { value: 'nightly' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith(expect.stringContaining('search=nightly'), expect.anything()))
  })

  it('cancels a selected run after confirmation and reports the outcome', async () => {
    apiGet.mockImplementation((path: string) => {
      if (path.startsWith('/api/v1/runs')) {
        return Promise.resolve({ items: [run('run-1', { status: 'running' })], total: 1, limit: 25, offset: 0, has_more: false })
      }
      if (path.startsWith('/api/v1/workflows')) return Promise.resolve({ items: [], total: 0 })
      return Promise.resolve({})
    })
    apiPost.mockResolvedValue({ id: 'run-1', status: 'cancelling' })
    renderPage()
    await screen.findAllByText('Nightly job')

    fireEvent.click(screen.getByLabelText('Select run run-1'))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel selected' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel 1' }))

    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/runs/run-1/cancel'))
    expect(await screen.findByText(/Last bulk action: 1 cancelled/)).toBeInTheDocument()
  })

  it('saves a filter to the server and renders it as a chip', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('My failures')
    apiPost.mockResolvedValue({ id: 'filter-1', name: 'My failures', filters: { status: 'failed' } })
    renderPage()
    await screen.findAllByText('Nightly job')

    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'failed' } })
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith(expect.stringContaining('status=failed'), expect.anything()))
    fireEvent.click(screen.getByRole('button', { name: 'Save filter' }))

    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/saved-filters', {
      name: 'My failures',
      filters: expect.objectContaining({ status: 'failed' }),
    }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'My failures' })).toBeInTheDocument())
  })

  it('paginates forward when the API reports more pages', async () => {
    apiGet.mockImplementation((path: string) => {
      if (path.includes('offset=25')) return Promise.resolve({ items: [run('run-9')], total: 26, limit: 25, offset: 25, has_more: false })
      if (path.startsWith('/api/v1/runs')) return Promise.resolve({ items: [run('run-1')], total: 26, limit: 25, offset: 0, has_more: true })
      return Promise.resolve({ items: [], total: 0 })
    })
    renderPage()
    await screen.findAllByText('Nightly job')
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith(expect.stringContaining('offset=25'), expect.anything()))
  })
})
