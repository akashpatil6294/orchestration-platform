/**
 * Authentication state for the whole application.
 *
 * State comes from Supabase, which owns the Google round trip and the session.
 * The backend independently verifies the token it receives; nothing here is
 * trusted for authorisation, it only drives the interface.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import type { Session, User } from '@supabase/supabase-js'

import { REDIRECT_STORAGE_KEY, configurationProblems } from '../lib/config'
import { getSupabase } from '../lib/supabase'

export interface AuthContextValue {
  user: User | null
  session: Session | null
  /** True until the stored session has been read for the first time. */
  loading: boolean
  /** True while the browser is being sent to Google. */
  redirecting: boolean
  error: string | null
  signInWithGoogle: (options?: { redirectTo?: string }) => Promise<void>
  signOut: () => Promise<void>
  clearError: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

/** Supabase processes the OAuth callback on the public login route. */
function defaultRedirectTo(): string {
  return `${window.location.origin}/login`
}

/** Remember the page a signed-out visitor wanted, so sign-in returns them there. */
export function rememberDestination(path: string | undefined): void {
  if (!path) return
  try {
    window.sessionStorage.setItem(REDIRECT_STORAGE_KEY, path)
  } catch {
    // Private browsing can disable storage; the default landing page is fine.
  }
}

export function consumeDestination(): string | null {
  try {
    const value = window.sessionStorage.getItem(REDIRECT_STORAGE_KEY)
    window.sessionStorage.removeItem(REDIRECT_STORAGE_KEY)
    return value
  } catch {
    return null
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null)
  // An unconfigured deployment can be detected during the first render, so it
  // never has to start in a loading state it would immediately leave.
  const [loading, setLoading] = useState(() => getSupabase() !== null)
  const [redirecting, setRedirecting] = useState(false)
  const [error, setError] = useState<string | null>(() =>
    getSupabase()
      ? null
      : configurationProblems.join(' ') || 'Single sign-on is not configured for this deployment.',
  )
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    const supabase = getSupabase()
    if (!supabase) {
      return () => {
        mounted.current = false
      }
    }

    // Subscribe before reading the snapshot so callback events cannot be lost.
    // A later snapshot must not overwrite a newer session from an auth event.
    let receivedAuthEvent = false
    const { data } = supabase.auth.onAuthStateChange((_event, nextSession) => {
      if (!mounted.current) return
      receivedAuthEvent = true
      setSession(nextSession)
      setLoading(false)
      if (nextSession) setError(null)
    })

    supabase.auth
      .getSession()
      .then(({ data, error: sessionError }) => {
        if (sessionError) throw sessionError
        if (!mounted.current) return
        if (!receivedAuthEvent) setSession(data.session ?? null)
      })
      .catch(() => {
        if (mounted.current && !receivedAuthEvent) {
          setError('Could not restore your previous session.')
        }
      })
      .finally(() => {
        if (mounted.current) setLoading(false)
      })

    return () => {
      mounted.current = false
      data.subscription.unsubscribe()
    }
  }, [])

  const signInWithGoogle = useCallback(async (options?: { redirectTo?: string }) => {
    const supabase = getSupabase()
    if (!supabase) {
      setError('Google sign-in is unavailable: this deployment is missing its Supabase configuration.')
      return
    }
    setError(null)
    setRedirecting(true)
    try {
      const { error: oauthError } = await supabase.auth.signInWithOAuth({
        provider: 'google',
        options: {
          redirectTo: options?.redirectTo ?? defaultRedirectTo(),
          queryParams: { prompt: 'select_account' },
        },
      })
      if (oauthError) {
        setError(oauthError.message || 'Google sign-in could not be started. Please try again.')
        setRedirecting(false)
      }
      // On success the browser is navigating away, so the button stays disabled.
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Google sign-in could not be started.')
      setRedirecting(false)
    }
  }, [])

  const signOut = useCallback(async () => {
    const supabase = getSupabase()
    setError(null)
    if (!supabase) {
      setSession(null)
      return
    }
    await supabase.auth.signOut()
    setSession(null)
  }, [])

  const clearError = useCallback(() => setError(null), [])

  const value = useMemo<AuthContextValue>(
    () => ({
      user: session?.user ?? null,
      session,
      loading,
      redirecting,
      error,
      signInWithGoogle,
      signOut,
      clearError,
    }),
    [session, loading, redirecting, error, signInWithGoogle, signOut, clearError],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth must be used inside <AuthProvider>')
  }
  return context
}

/** Friendly display name, falling back to the local part of the address. */
export function displayNameFor(user: User | null): string {
  if (!user) return ''
  const metadata = (user.user_metadata ?? {}) as Record<string, unknown>
  const candidates = [metadata.full_name, metadata.name, metadata.display_name, metadata.preferred_username]
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim()) return candidate.trim()
  }
  return user.email?.split('@')[0] ?? 'Signed in'
}

/** The Google profile picture, when the provider supplied one. */
export function avatarUrlFor(user: User | null): string | null {
  if (!user) return null
  const metadata = (user.user_metadata ?? {}) as Record<string, unknown>
  const candidate = metadata.avatar_url ?? metadata.picture
  return typeof candidate === 'string' && candidate ? candidate : null
}

export function initialsFor(user: User | null): string {
  const name = displayNameFor(user).replace(/[@._-]+/g, ' ').trim()
  if (!name) return '?'
  const parts = name.split(/\s+/).filter(Boolean)
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
}
