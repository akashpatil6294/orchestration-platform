import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { ToastProvider } from '../components/ToastProvider'
import TemplatesPage from './TemplatesPage'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())

vi.mock('../lib/api', () => ({
  ApiError: class ApiError extends Error {},
  api: { get: apiGet, post: apiPost },
}))

const packs = [
  {
    id: 'log-digest',
    name: 'Error log digest',
    category: 'Operations',
    description: 'Fetch logs and email a digest.',
    connections_required: [],
    step_count: 4,
    tags: ['logs'],
  },
  {
    id: 'backup-verify',
    name: 'Backup verification',
    category: 'Operations',
    description: 'Verify backups and alert.',
    connections_required: [{ name: 'team-slack', kind: 'slack_webhook', label: 'Slack webhook' }],
    step_count: 4,
    tags: ['backup'],
  },
]

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <TemplatesPage />
      </ToastProvider>
    </MemoryRouter>,
  )
}

describe('TemplatesPage', () => {
  it('lists the workflow packs grouped by category', async () => {
    apiGet.mockResolvedValue({ items: packs })
    renderPage()
    await waitFor(() => expect(screen.getByText('Error log digest')).toBeTruthy())
    expect(screen.getByText('Operations')).toBeTruthy()
    expect(screen.getByText('Backup verification')).toBeTruthy()
  })

  it('shows required connections for packs that need them', async () => {
    apiGet.mockResolvedValue({ items: packs })
    renderPage()
    await waitFor(() => expect(screen.getByText('Error log digest')).toBeTruthy())
    expect(screen.getByText('No connections needed')).toBeTruthy()
    expect(screen.getByText('Needs 1 connection')).toBeTruthy()
    expect(screen.getByText('team-slack')).toBeTruthy()
  })

  it('installs a pack when the install button is clicked', async () => {
    apiGet.mockResolvedValue({ items: packs })
    apiPost.mockResolvedValue({ id: 'wf-1' })
    const { container } = renderPage()
    await waitFor(() => expect(screen.getByText('Error log digest')).toBeTruthy())
    const buttons = container.querySelectorAll('button')
    const install = Array.from(buttons).find((b) => b.textContent === 'Install')
    expect(install).toBeTruthy()
  })
})
