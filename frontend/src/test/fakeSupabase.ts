/**
 * A controllable stand-in for the Supabase browser client.
 *
 * Tests never reach the network: they drive sign-in and sign-out through this
 * fake so the auth state machinery can be exercised deterministically.
 */
import { vi } from 'vitest'
import type { Session } from '@supabase/supabase-js'

export function fakeSession(overrides: Partial<{ email: string; name: string; avatar: string }> = {}): Session {
  const email = overrides.email ?? 'ada@example.com'
  const session = {
    access_token: 'fake-access-token',
    refresh_token: 'fake-refresh-token',
    token_type: 'bearer',
    expires_in: 3600,
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    user: {
      id: 'supabase-user-1',
      aud: 'authenticated',
      role: 'authenticated',
      email,
      app_metadata: { provider: 'google' },
      user_metadata: {
        full_name: overrides.name ?? 'Ada Lovelace',
        avatar_url: overrides.avatar ?? 'https://example.test/avatar.png',
      },
      created_at: new Date('2026-01-01T00:00:00Z').toISOString(),
    },
  }
  return session as unknown as Session
}

export interface FakeSupabase {
  auth: {
    getSession: ReturnType<typeof vi.fn>
    refreshSession: ReturnType<typeof vi.fn>
    signInWithOAuth: ReturnType<typeof vi.fn>
    signOut: ReturnType<typeof vi.fn>
    onAuthStateChange: ReturnType<typeof vi.fn>
  }
  /** Simulate Supabase pushing an auth state change. */
  emit: (event: string, session: Session | null) => void
  signInWithOAuthCalls: unknown[]
}

export function fakeSupabase(
  options: {
    initialSession?: Session | null
    oauthError?: { message: string } | null
    onSignIn?: () => void
    refreshSession?: Session | null
  } = {},
): FakeSupabase {
  let current: Session | null = options.initialSession ?? null
  let listener: ((event: string, session: Session | null) => void) | null = null
  const signInWithOAuthCalls: unknown[] = []

  const fake: FakeSupabase = {
    signInWithOAuthCalls,
    emit(event, session) {
      current = session
      listener?.(event, session)
    },
    auth: {
      getSession: vi.fn(async () => ({ data: { session: current }, error: null })),
      refreshSession: vi.fn(async () => {
        if (options.refreshSession === undefined) return { data: { session: current }, error: null }
        current = options.refreshSession
        return { data: { session: options.refreshSession }, error: null }
      }),
      signInWithOAuth: vi.fn(async (args: unknown) => {
        signInWithOAuthCalls.push(args)
        options.onSignIn?.()
        if (options.oauthError) return { data: null, error: options.oauthError }
        return { data: { provider: 'google', url: 'https://accounts.google.com/o/oauth2/auth' }, error: null }
      }),
      signOut: vi.fn(async () => {
        current = null
        listener?.('SIGNED_OUT', null)
        return { error: null }
      }),
      onAuthStateChange: vi.fn((callback: (event: string, session: Session | null) => void) => {
        listener = callback
        return { data: { subscription: { unsubscribe: vi.fn() } } }
      }),
    },
  }
  return fake
}
