/** No-dead-buttons test for the dead-letter page. */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ConfirmProvider } from '../components/ConfirmDialogProvider'
import { ToastProvider } from '../components/ToastProvider'
import { DlqPage } from './DlqPage'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())

vi.mock('../lib/api', () => ({
  ApiError: class ApiError extends Error {},
  api: { get: apiGet, post: apiPost, patch: vi.fn(), put: vi.fn(), remove: vi.fn() },
}))

const entry = {
  step_run_id: 'step-1',
  run_id: 'run-1',
  workflow_id: 'workflow-1',
  workflow_name: 'Nightly job',
  step_key: 'extract',
  task_type: 'document.extract_text',
  attempts: 3,
  retry_limit: 2,
  lease_expirations: 0,
  priority: 0,
  queue: 'default',
  error: { code: 'runtime_error', message: 'boom' },
  failed_at: '2026-10-05T10:00:00Z',
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <ConfirmProvider>
          <DlqPage />
        </ConfirmProvider>
      </ToastProvider>
    </MemoryRouter>,
  )
}

describe('DlqPage', () => {
  beforeEach(() => {
    apiGet.mockReset()
    apiPost.mockReset()
    apiGet.mockResolvedValue({ items: [entry] })
    apiPost.mockResolvedValue({ run_id: 'run-1', step_run_id: 'step-1', status: 'retrying', already_redriven: false, retried_steps: ['extract'], reset_steps: [] })
  })

  it('redrives a dead letter and refreshes the list', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Redrive' }))

    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/dlq/step-1/redrive'))
    expect(await screen.findByRole('status')).toBeInTheDocument()
  })

  it('reports already-scheduled redrives as a notice instead of an error', async () => {
    apiPost.mockResolvedValue({ run_id: 'run-1', step_run_id: 'step-1', status: 'retrying', already_redriven: true, retried_steps: [], reset_steps: [] })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Redrive' }))

    await waitFor(() => expect(apiPost).toHaveBeenCalled())
    expect(await screen.findByRole('status')).toHaveTextContent(/already/i)
  })

  it('shows the stored error details on demand', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Show error' }))
    expect(await screen.findByText(/boom/)).toBeInTheDocument()
  })

  it('bulk redrives selected entries after confirmation', async () => {
    renderPage()
    await screen.findByText('Nightly job')
    fireEvent.click(screen.getByLabelText('Select step extract'))
    fireEvent.click(screen.getByRole('button', { name: 'Redrive selected' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Redrive all' }))

    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/dlq/step-1/redrive'))
    expect(await screen.findByRole('status')).toBeInTheDocument()
  })

  it('filters entries by workflow name', async () => {
    renderPage()
    await screen.findByText('Nightly job')
    fireEvent.change(screen.getByLabelText('Filter dead letters'), { target: { value: 'nothing-matches' } })
    expect(await screen.findByText('Nothing matches that filter')).toBeInTheDocument()
  })
})
