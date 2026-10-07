import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { REDIRECT_STORAGE_KEY } from '../lib/config'
import { fakeSession, fakeSupabase } from '../test/fakeSupabase'
import { AuthProvider } from '../auth/AuthProvider'
import { LoginPage } from './LoginPage'

const fake = { current: fakeSupabase() }

vi.mock('../lib/supabase', () => ({
  getSupabase: () => fake.current,
  resetSupabaseClient: () => {},
}))

/** The dashboard stands in for "a protected page the visitor can reach". */
function renderLogin() {
  return render(
    <MemoryRouter initialEntries={['/login']}>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/dashboard" element={<h1>Dashboard</h1>} />
          <Route path="/workflows/:workflowId" element={<h1>Workflow detail</h1>} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('LoginPage', () => {
  beforeEach(() => {
    fake.current = fakeSupabase()
  })

  it('presents the product and a single Google action', async () => {
    renderLogin()

    expect(screen.getByRole('heading', { name: /Build, execute, monitor and retry reliable workflows/i })).toBeInTheDocument()
    expect(screen.getByText('Workflow Orchestration Platform')).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /Continue with Google/i })).toBeEnabled()
  })

  it('shows a loading state until the stored session has been read', async () => {
    // A session read that has not resolved yet: the visitor must see progress
    // rather than a button that would interrupt it.
    fake.current = fakeSupabase()
    fake.current.auth.getSession = vi.fn(() => new Promise(() => {}))

    renderLogin()

    const pending = await screen.findByRole('button', { name: /Checking your session/i })
    expect(pending).toBeDisabled()
  })

  it('starts Google sign-in when the button is pressed', async () => {
    renderLogin()
    const button = await screen.findByRole('button', { name: /Continue with Google/i })

    await userEvent.click(button)

    await waitFor(() => expect(fake.current.auth.signInWithOAuth).toHaveBeenCalledTimes(1))
    const call = fake.current.signInWithOAuthCalls[0] as { provider: string }
    expect(call.provider).toBe('google')
  })

  it('disables the button and shows progress once a redirect is under way', async () => {
    let resolveOAuth: () => void = () => {}
    fake.current = fakeSupabase({
      onSignIn: () => {
        // The provider call stays pending, exactly as it does while the browser
        // is being sent to Google.
      },
    })
    fake.current.auth.signInWithOAuth = vi.fn(
      () =>
        new Promise<{ error: null }>((resolve) => {
          resolveOAuth = () => resolve({ error: null })
        }),
    )

    renderLogin()
    const button = await screen.findByRole('button', { name: /Continue with Google/i })
    await userEvent.click(button)

    const busy = await screen.findByRole('button', { name: /Redirecting to Google/i })
    expect(busy).toBeDisabled()

    resolveOAuth()
  })

  it('shows an authentication error to the visitor', async () => {
    fake.current = fakeSupabase({ oauthError: { message: 'Access blocked: this app is not verified' } })
    renderLogin()

    await userEvent.click(await screen.findByRole('button', { name: /Continue with Google/i }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Access blocked: this app is not verified')
    // The button becomes usable again so the visitor can retry.
    await waitFor(() => expect(screen.getByRole('button', { name: /Continue with Google/i })).toBeEnabled())
  })

  it('does not leak implementation details to the visitor', async () => {
    renderLogin()
    await screen.findByRole('button', { name: /Continue with Google/i })

    const text = document.body.textContent ?? ''
    expect(text).not.toMatch(/client_secret|service_role|anon key|VITE_|supabase key/i)
  })

  it('sends an already signed-in visitor to the page they wanted', async () => {
    fake.current = fakeSupabase({ initialSession: fakeSession() })
    window.sessionStorage.setItem(REDIRECT_STORAGE_KEY, '/dashboard')

    renderLogin()

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Dashboard' })).toBeInTheDocument())
  })

  it('uses the saved destination without issuing a competing dashboard redirect', async () => {
    fake.current = fakeSupabase({ initialSession: fakeSession() })
    window.sessionStorage.setItem(REDIRECT_STORAGE_KEY, '/workflows/abc')

    renderLogin()

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Workflow detail' })).toBeInTheDocument())
  })

  it('routes a callback session to the dashboard when no destination was saved', async () => {
    fake.current = fakeSupabase()
    renderLogin()

    await waitFor(() => expect(fake.current.auth.onAuthStateChange).toHaveBeenCalled())
    fake.current.emit('SIGNED_IN', fakeSession())

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Dashboard' })).toBeInTheDocument())
  })
})
