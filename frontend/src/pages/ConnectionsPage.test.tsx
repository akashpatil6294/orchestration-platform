import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { ToastProvider } from '../components/ToastProvider'
import ConnectionsPage from './ConnectionsPage'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiRemove = vi.hoisted(() => vi.fn())

vi.mock('../lib/api', () => ({
  ApiError: class ApiError extends Error {},
  api: { get: apiGet, post: apiPost, remove: apiRemove },
}))

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <ConnectionsPage />
      </ToastProvider>
    </MemoryRouter>,
  )
}

describe('ConnectionsPage', () => {
  it('lists saved connections without ever showing values', async () => {
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/v1/connections') {
        return Promise.resolve({
          items: [{ id: 'c1', name: 'team-slack', kind: 'slack_webhook', team_id: null, owner_id: 'u1', created_at: '' }],
        })
      }
      if (path === '/api/v1/teams') return Promise.resolve({ items: [] })
      return Promise.resolve({ kinds: ['slack_webhook', 'sql_url', 'generic'] })
    })
    const { container } = renderPage()
    await waitFor(() => expect(screen.getByText('team-slack')).toBeTruthy())
    expect(screen.getAllByText('Slack webhook').length).toBeGreaterThanOrEqual(1)
    // The value must never appear in the DOM.
    expect(container.textContent).not.toContain('https://hooks.slack.com')
  })

  it('shows the $connection reference syntax hint', async () => {
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/v1/connections') return Promise.resolve({ items: [] })
      if (path === '/api/v1/teams') return Promise.resolve({ items: [] })
      return Promise.resolve({ kinds: ['generic'] })
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('No connections yet.')).toBeTruthy())
    expect(screen.getByText('New connection')).toBeTruthy()
  })
})
