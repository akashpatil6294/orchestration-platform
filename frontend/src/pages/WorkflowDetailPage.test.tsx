/** No-dead-buttons test for the workflow detail page. */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ConfirmProvider } from '../components/ConfirmDialogProvider'
import { ToastProvider } from '../components/ToastProvider'
import { WorkflowDetailPage } from './WorkflowDetailPage'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiPut = vi.hoisted(() => vi.fn())
const apiRemove = vi.hoisted(() => vi.fn())

vi.mock('../lib/api', () => ({
  ApiError: class ApiError extends Error {},
  api: { get: apiGet, post: apiPost, patch: vi.fn(), put: apiPut, remove: apiRemove },
}))

const workflow = {
  id: 'workflow-1',
  user_role: 'owner',
  name: 'Nightly job',
  description: 'A demo workflow',
  latest_version: 2,
  step_count: 2,
  archived: false,
  default_max_parallel: 4,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-02T00:00:00Z',
  run_counts: {},
  last_run_at: null,
  last_run_status: null,
  has_draft_changes: false,
  draft: {
    name: 'Nightly job',
    description: '',
    default_max_parallel: 4,
    tags: [],
    steps: [
      { id: 'a', type: 'demo.echo', input: {}, depends_on: [], retries: 0, timeout_seconds: 60 },
      { id: 'b', type: 'approval', input: {}, depends_on: ['a'], retries: 0, timeout_seconds: 600 },
    ],
  },
  versions: [
    { version: 2, published_at: '2026-10-02T00:00:00Z', published_by: 'ops@example.com', publish_note: 'tune retries', step_count: 2, definition_hash: 'abc123def456', is_latest: true },
    { version: 1, published_at: '2026-10-01T00:00:00Z', published_by: null, publish_note: '', step_count: 1, definition_hash: 'aaa111bbb222', is_latest: false },
  ],
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/workflows/workflow-1']}>
      <ToastProvider>
        <ConfirmProvider>
          <Routes>
            <Route path="/workflows/:workflowId" element={<WorkflowDetailPage />} />
            <Route path="/workflows" element={<div>workflows index</div>} />
          </Routes>
        </ConfirmProvider>
      </ToastProvider>
    </MemoryRouter>,
  )
}

describe('WorkflowDetailPage', () => {
  beforeEach(() => {
    apiGet.mockReset()
    apiPost.mockReset()
    apiPut.mockReset()
    apiRemove.mockReset()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/v1/workflows/workflow-1') return Promise.resolve(workflow)
      if (path === '/api/v1/workflows/workflow-1/runs?limit=10') return Promise.resolve({ items: [], total: 0 })
      if (path.startsWith('/api/v1/schedules')) return Promise.resolve({ items: [], total: 0 })
      if (path === '/api/v1/workflows/workflow-1/secrets') {
        return Promise.resolve([{ name: 'GROQ_API_KEY', created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z' }])
      }
      if (path.startsWith('/api/v1/workflows/workflow-1/versions/')) {
        return Promise.resolve({ ...workflow.versions[0], definition: workflow.draft })
      }
      return Promise.resolve({})
    })
    apiPost.mockResolvedValue({})
  })

  it('publishes a draft and shows the notice', async () => {
    apiPost.mockResolvedValue({ version: 3 })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Publish draft' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/workflows/workflow-1/publish', { note: '' }))
    expect(await screen.findByText('Published version 3.')).toBeInTheDocument()
  })

  it('archives a workflow after confirmation', async () => {
    renderPage()
    await screen.findAllByText('Nightly job')
    fireEvent.click(screen.getByRole('button', { name: 'Archive' }))
    const dialog = await screen.findByRole('dialog', { name: 'Archive workflow' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith('/api/v1/workflows/workflow-1/archive?archived=true'))
  })

  it('deletes a workflow after confirmation and navigates away', async () => {
    apiRemove.mockResolvedValue(undefined)
    renderPage()
    await screen.findAllByText('Nightly job')
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Delete workflow' }))
    await waitFor(() => expect(apiRemove).toHaveBeenCalledWith('/api/v1/workflows/workflow-1'))
    expect(await screen.findByText('workflows index')).toBeInTheDocument()
  })

  it('lists secrets write-only: shows the name, never a value input with content', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Show secrets' }))
    expect(await screen.findByText('GROQ_API_KEY')).toBeInTheDocument()
    const valueInput = screen.getByLabelText('Secret value') as HTMLInputElement
    expect(valueInput.type).toBe('password')
    expect(valueInput.value).toBe('')
  })

  it('saves a secret through PUT with name and value', async () => {
    apiPut.mockResolvedValue({ name: 'SLACK_TOKEN', created_at: '', updated_at: '' })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Show secrets' }))
    fireEvent.change(await screen.findByLabelText('Secret name'), { target: { value: 'SLACK_TOKEN' } })
    fireEvent.change(screen.getByLabelText('Secret value'), { target: { value: 'xoxb-secret' } })
    fireEvent.click(screen.getByRole('button', { name: 'Set secret' }))

    await waitFor(() => expect(apiPut).toHaveBeenCalledWith('/api/v1/workflows/workflow-1/secrets/SLACK_TOKEN', { name: 'SLACK_TOKEN', value: 'xoxb-secret' }))
  })

  it('deletes a secret after confirmation', async () => {
    apiRemove.mockResolvedValue(undefined)
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Show secrets' }))
    await screen.findByText('GROQ_API_KEY')
    const secretsPanel = screen.getByRole('region', { name: 'Secrets' })
    fireEvent.click(within(secretsPanel).getByRole('button', { name: 'Delete' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Delete secret' }))
    await waitFor(() => expect(apiRemove).toHaveBeenCalledWith('/api/v1/workflows/workflow-1/secrets/GROQ_API_KEY'))
  })

  it('opens a published version and shows its definition', async () => {
    renderPage()
    await screen.findAllByText('Nightly job')
    fireEvent.click(screen.getAllByRole('button', { name: 'View definition' })[0])
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/v1/workflows/workflow-1/versions/2'))
    expect(await screen.findByText(/Version 2 · hash abc123def456/)).toBeInTheDocument()
  })
})
