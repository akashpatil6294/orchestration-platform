import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ConfirmProvider } from '../components/ConfirmDialogProvider'
import { ToastProvider } from '../components/ToastProvider'
import { SchedulesPage } from './SchedulesPage'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiRemove = vi.hoisted(() => vi.fn())

vi.mock('../lib/api', () => ({
  ApiError: class ApiError extends Error {},
  api: { get: apiGet, post: apiPost, remove: apiRemove },
}))

const schedule = {
  id: 'schedule-1', workflow_id: 'workflow-1', workflow_name: 'Nightly job', name: 'Nightly',
  cron_expression: '0 7 * * *', cron_description: 'Daily at 07:00', timezone: 'UTC', enabled: true,
  version: 1, effective_version: 1, input: { env: 'prod' }, overlap_policy: 'skip', catchup: false,
  data_interval_seconds: null, jitter_seconds: 0, skip_weekends: false, skip_dates: [], pause_windows: [],
  next_run_at: '2026-10-06T07:00:00Z', last_run_at: null, last_run_id: null, last_status: null,
  run_count: 0, last_error: null, upcoming: [], created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
}

describe('SchedulesPage', () => {
  beforeEach(() => {
    apiGet.mockReset()
    apiPost.mockReset()
    apiRemove.mockReset()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/v1/schedules') return Promise.resolve({ items: [schedule], total: 1 })
      if (path === '/api/v1/workflows') return Promise.resolve({ items: [], total: 0 })
      if (path.endsWith('/backfills')) return Promise.resolve({ items: [], total: 0 })
      return Promise.resolve({})
    })
    apiPost.mockResolvedValue({ id: 'backfill-1', status: 'running' })
  })

  it('sends an inclusive end date as the API half-open UTC range', async () => {
    render(
      <MemoryRouter>
        <ToastProvider>
          <ConfirmProvider>
            <SchedulesPage />
          </ConfirmProvider>
        </ToastProvider>
      </MemoryRouter>,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Backfill' }))
    fireEvent.change(await screen.findByLabelText('Start date (UTC)'), { target: { value: '2026-01-03' } })
    fireEvent.change(screen.getByLabelText('End date (UTC, inclusive)'), { target: { value: '2026-01-03' } })
    fireEvent.change(screen.getByLabelText('Concurrency'), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start backfill' }))

    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/schedules/schedule-1/backfill', {
      start: '2026-01-03T00:00:00.000Z',
      end: '2026-01-04T00:00:00.000Z',
      concurrency_limit: 3,
    }))
  })

  it('deletes a schedule only after confirmation', async () => {
    apiRemove.mockResolvedValue(undefined)
    render(
      <MemoryRouter>
        <ToastProvider>
          <ConfirmProvider>
            <SchedulesPage />
          </ConfirmProvider>
        </ToastProvider>
      </MemoryRouter>,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    const dialog = await screen.findByRole('dialog', { name: 'Delete schedule' })
    expect(apiRemove).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete schedule' }))
    await waitFor(() => expect(apiRemove).toHaveBeenCalledWith('/api/v1/schedules/schedule-1'))
  })
})
