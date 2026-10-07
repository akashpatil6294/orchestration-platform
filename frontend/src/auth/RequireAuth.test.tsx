import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { REDIRECT_STORAGE_KEY } from '../lib/config'
import { fakeSession, fakeSupabase } from '../test/fakeSupabase'
import { AuthProvider } from './AuthProvider'
import { RequireAuth } from './RequireAuth'

const fake = { current: fakeSupabase() }

vi.mock('../lib/supabase', () => ({
  getSupabase: () => fake.current,
  resetSupabaseClient: () => {},
}))

function renderApp() {
  return render(
    <MemoryRouter initialEntries={['/workflows/abc?tab=runs']}>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<h1>Sign in page</h1>} />
          <Route element={<RequireAuth />}>
            <Route path="/workflows/:workflowId" element={<h1>Workflow detail</h1>} />
          </Route>
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('RequireAuth', () => {
  beforeEach(() => {
    fake.current = fakeSupabase()
  })

  it('redirects an unauthenticated visitor to the login page', async () => {
    renderApp()
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Sign in page' })).toBeInTheDocument())
  })

  it('remembers where the visitor was heading', async () => {
    renderApp()
    await waitFor(() => expect(window.sessionStorage.getItem(REDIRECT_STORAGE_KEY)).toBe('/workflows/abc?tab=runs'))
  })

  it('renders the protected page for an authenticated visitor', async () => {
    fake.current = fakeSupabase({ initialSession: fakeSession() })
    renderApp()

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Workflow detail' })).toBeInTheDocument())
    expect(screen.queryByRole('heading', { name: 'Sign in page' })).not.toBeInTheDocument()
  })

  it('shows a loading state instead of flashing the login page', async () => {
    renderApp()
    expect(screen.getByRole('status')).toHaveTextContent('Checking your session')
    // Settle the session read so the state update happens inside act().
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Sign in page' })).toBeInTheDocument())
  })
})
