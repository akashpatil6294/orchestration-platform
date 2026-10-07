import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ConfirmProvider } from '../components/ConfirmDialogProvider'
import { ToastProvider } from '../components/ToastProvider'
import { TriggersPage } from './TriggersPage'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiPatch = vi.hoisted(() => vi.fn())
const apiRemove = vi.hoisted(() => vi.fn())

vi.mock('../lib/api', () => ({
  ApiError: class ApiError extends Error {},
  api: { get: apiGet, post: apiPost, patch: apiPatch, remove: apiRemove },
}))

const workflow = {
  id: 'workflow-1', name: 'Published workflow', description: '', latest_version: 2, default_max_parallel: 2,
  has_draft_changes: false, step_count: 1, schedule_count: 0, last_run_status: null,
  last_run_at: null, archived: false, run_counts: {}, created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
}

describe('TriggersPage', () => {
  beforeEach(() => {
    apiGet.mockReset()
    apiPost.mockReset()
    apiPatch.mockReset()
    apiRemove.mockReset()
    apiGet.mockImplementation((path: string) => path === '/api/v1/triggers'
      ? Promise.resolve({ items: [], total: 0 })
      : Promise.resolve({ items: [workflow], total: 1 }))
  })

  it('creates a signed webhook and displays its signing secret once', async () => {
    apiPost.mockResolvedValue({
      id: 'trigger-1', workflow_id: workflow.id, workflow_name: workflow.name, kind: 'webhook',
      name: 'Orders', source_workflow_id: null, version: 2, input_mapping: { order_id: 'payload.data.id' },
      enabled: true, rate_limit_per_minute: 60, endpoint: '/api/v1/hooks/trigger-1',
      created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    })

    render(
      <ToastProvider>
        <ConfirmProvider>
          <TriggersPage />
        </ConfirmProvider>
      </ToastProvider>,
    )
    fireEvent.change(await screen.findByLabelText('Target workflow'), { target: { value: workflow.id } })
    fireEvent.change(screen.getByLabelText('Name (optional)'), { target: { value: 'Orders' } })
    fireEvent.change(screen.getByLabelText('Input mapping'), { target: { value: '{"order_id":"payload.data.id"}' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create trigger' }))

    const shownSecret = (await screen.findByLabelText('Signing secret') as HTMLInputElement).value
    expect(shownSecret).toMatch(/^[a-f0-9]{64}$/)
    const [, request] = apiPost.mock.calls[0]
    expect(request).toMatchObject({
      name: 'Orders', kind: 'webhook', input_mapping: { order_id: 'payload.data.id' },
      rate_limit_per_minute: 60, signing_secret: expect.stringMatching(/^[a-f0-9]{64}$/),
    })
  })

  it('does not send an invalid input mapping', async () => {
    render(
      <ToastProvider>
        <ConfirmProvider>
          <TriggersPage />
        </ConfirmProvider>
      </ToastProvider>,
    )
    fireEvent.change(await screen.findByLabelText('Target workflow'), { target: { value: workflow.id } })
    fireEvent.change(screen.getByLabelText('Input mapping'), { target: { value: '[not-an-object]' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create trigger' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Input mapping')
    await waitFor(() => expect(apiPost).not.toHaveBeenCalled())
  })

  it('deletes a trigger only after confirmation', async () => {
    apiGet.mockImplementation((path: string) =>
      path === '/api/v1/triggers'
        ? Promise.resolve({ items: [{ id: 'trigger-1', workflow_id: 'workflow-1', workflow_name: 'Published workflow', kind: 'webhook', name: 'Orders', source_workflow_id: null, version: 2, input_mapping: {}, enabled: true, rate_limit_per_minute: 60, endpoint: '/api/v1/hooks/trigger-1', created_at: new Date().toISOString(), updated_at: new Date().toISOString() }], total: 1 })
        : Promise.resolve({ items: [workflow], total: 1 }),
    )
    apiRemove.mockResolvedValue(undefined)
    render(
      <ToastProvider>
        <ConfirmProvider>
          <TriggersPage />
        </ConfirmProvider>
      </ToastProvider>,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    const dialog = await screen.findByRole('dialog', { name: 'Delete trigger' })
    expect(apiRemove).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete trigger' }))
    await waitFor(() => expect(apiRemove).toHaveBeenCalledWith('/api/v1/triggers/trigger-1'))
  })
})
