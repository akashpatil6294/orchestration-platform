import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AuthProvider } from '../auth/AuthProvider'
import { fakeSession, fakeSupabase } from '../test/fakeSupabase'
import { AppShell } from './AppShell'

const fake = { current: fakeSupabase() }

vi.mock('../lib/supabase', () => ({
  getSupabase: () => fake.current,
  resetSupabaseClient: () => {},
}))

function renderShell() {
  return render(
    <MemoryRouter initialEntries={['/dashboard']}>
      <AuthProvider>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/dashboard" element={<h1>Dashboard</h1>} />
          </Route>
          <Route path="/login" element={<h1>Sign in page</h1>} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('AppShell', () => {
  beforeEach(() => {
    fake.current = fakeSupabase({
      initialSession: fakeSession({ email: 'ada@example.com', name: 'Ada Lovelace' }),
    })
  })

  it('shows the Google profile name, address and avatar', async () => {
    renderShell()

    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())
    expect(screen.getByText('ada@example.com')).toBeInTheDocument()
    const avatar = document.querySelector('img.avatar') as HTMLImageElement
    expect(avatar.src).toBe('https://example.test/avatar.png')
    // The picture is loaded cross-origin without sending credentials or a referrer.
    expect(avatar.getAttribute('referrerpolicy')).toBe('no-referrer')
  })

  it('falls back to initials when the provider supplied no picture', async () => {
    const session = fakeSession()
    session.user.user_metadata = { full_name: 'Grace Hopper' }
    fake.current = fakeSupabase({ initialSession: session })

    renderShell()

    await waitFor(() => expect(screen.getByText('GH')).toBeInTheDocument())
  })

  it('signs the user out from the account menu', async () => {
    renderShell()
    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeInTheDocument())

    await userEvent.click(screen.getByRole('button', { name: 'Account menu', expanded: false }))
    await userEvent.click(screen.getByRole('menuitem', { name: 'Sign out' }))

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Sign in page' })).toBeInTheDocument())
    expect(fake.current.auth.signOut).toHaveBeenCalled()
  })

  it('renders the routed page inside the shell', async () => {
    renderShell()
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Dashboard' })).toBeInTheDocument())
  })
})
