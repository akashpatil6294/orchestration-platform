import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { fakeSession, fakeSupabase } from '../test/fakeSupabase'
import { AuthProvider, displayNameFor, useAuth } from './AuthProvider'

const fake = { current: fakeSupabase() }

vi.mock('../lib/supabase', () => ({
  getSupabase: () => fake.current,
  resetSupabaseClient: () => {},
}))

function Probe() {
  const { user, session, loading, error, signInWithGoogle, signOut } = useAuth()
  return (
    <div>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="email">{user?.email ?? 'none'}</span>
      <span data-testid="session">{session ? 'present' : 'absent'}</span>
      <span data-testid="name">{displayNameFor(user)}</span>
      <span data-testid="error">{error ?? 'none'}</span>
      <button type="button" onClick={() => void signInWithGoogle()}>
        sign in
      </button>
      <button type="button" onClick={() => void signOut()}>
        sign out
      </button>
    </div>
  )
}

function renderProbe() {
  return render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  )
}

describe('AuthProvider', () => {
  beforeEach(() => {
    fake.current = fakeSupabase()
  })

  it('starts in a loading state and settles once the session is read', async () => {
    renderProbe()
    expect(screen.getByTestId('loading')).toHaveTextContent('true')

    await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'))
    expect(screen.getByTestId('session')).toHaveTextContent('absent')
  })

  it('exposes a session restored from storage', async () => {
    fake.current = fakeSupabase({ initialSession: fakeSession({ email: 'grace@example.com', name: 'Grace Hopper' }) })
    renderProbe()

    await waitFor(() => expect(screen.getByTestId('email')).toHaveTextContent('grace@example.com'))
    expect(screen.getByTestId('name')).toHaveTextContent('Grace Hopper')
    expect(screen.getByTestId('session')).toHaveTextContent('present')
  })

  it('follows Supabase auth state changes', async () => {
    fake.current = fakeSupabase()
    renderProbe()
    await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'))

    act(() => fake.current.emit('SIGNED_IN', fakeSession({ email: 'returned@example.com' })))
    await waitFor(() => expect(screen.getByTestId('email')).toHaveTextContent('returned@example.com'))

    act(() => fake.current.emit('SIGNED_OUT', null))
    await waitFor(() => expect(screen.getByTestId('session')).toHaveTextContent('absent'))
  })

  it('does not let a stale session snapshot overwrite a callback auth event', async () => {
    fake.current = fakeSupabase()
    let resolveSession: (value: { data: { session: null }; error: null }) => void = () => {}
    fake.current.auth.getSession = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveSession = resolve
        }),
    )
    renderProbe()

    await waitFor(() => expect(fake.current.auth.onAuthStateChange).toHaveBeenCalled())
    act(() => fake.current.emit('SIGNED_IN', fakeSession({ email: 'returned@example.com' })))
    await waitFor(() => expect(screen.getByTestId('email')).toHaveTextContent('returned@example.com'))

    await act(async () => resolveSession({ data: { session: null }, error: null }))

    expect(screen.getByTestId('email')).toHaveTextContent('returned@example.com')
    expect(screen.getByTestId('session')).toHaveTextContent('present')
  })

  it('starts Google OAuth with a callback to the login route', async () => {
    fake.current = fakeSupabase()
    renderProbe()
    await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'))

    await userEvent.click(screen.getByRole('button', { name: 'sign in' }))

    await waitFor(() => expect(fake.current.signInWithOAuthCalls).toHaveLength(1))
    expect(fake.current.signInWithOAuthCalls[0]).toMatchObject({
      provider: 'google',
      options: { redirectTo: `${window.location.origin}/login` },
    })
  })

  it('surfaces an OAuth failure instead of leaving the user waiting', async () => {
    fake.current = fakeSupabase({ oauthError: { message: 'provider is not enabled' } })
    renderProbe()
    await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'))

    await userEvent.click(screen.getByRole('button', { name: 'sign in' }))

    await waitFor(() => expect(screen.getByTestId('error')).toHaveTextContent('provider is not enabled'))
  })

  it('clears the session on sign out', async () => {
    fake.current = fakeSupabase({ initialSession: fakeSession() })
    renderProbe()
    await waitFor(() => expect(screen.getByTestId('session')).toHaveTextContent('present'))

    await userEvent.click(screen.getByRole('button', { name: 'sign out' }))

    await waitFor(() => expect(screen.getByTestId('session')).toHaveTextContent('absent'))
    expect(fake.current.auth.signOut).toHaveBeenCalled()
  })
})

describe('displayNameFor', () => {
  it('falls back to the email local part when no name is provided', () => {
    const session = fakeSession({ email: 'operator@example.com' })
    session.user.user_metadata = {}
    expect(displayNameFor(session.user)).toBe('operator')
  })

  it('returns an empty string for a signed-out visitor', () => {
    expect(displayNameFor(null)).toBe('')
  })
})
