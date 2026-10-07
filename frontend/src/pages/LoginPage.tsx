/**
 * The sign-in page.
 *
 * Google is the only offered method here: Supabase performs the OAuth exchange
 * and hands the browser a session, which the API then verifies on every request.
 */
import { useEffect } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { consumeDestination, useAuth } from '../auth/AuthProvider'
import { FullPageLoader } from '../components/StatusView'
import { configurationProblems, supabaseConfigured } from '../lib/config'

const HIGHLIGHTS = [
  'Versioned workflow graphs, published immutably',
  'Retries, leases and timeouts on every step',
  'Live run progress, attempt history and logs',
  'Timezone-aware recurring schedules',
]

export function LoginPage() {
  const { session, loading, redirecting, error, signInWithGoogle, clearError } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const destination = (location.state as { from?: string } | null)?.from

  // Coming back from Google leaves the browser on /login with a session in
  // place; move on to the page the visitor originally wanted.
  useEffect(() => {
    if (loading || !session) return
    navigate(destination ?? consumeDestination() ?? '/dashboard', { replace: true })
  }, [loading, session, destination, navigate])

  if (!loading && session) {
    return <FullPageLoader label="Opening your dashboard…" />
  }

  const busy = redirecting || loading

  return (
    <div className="login-layout">
      <section className="login-pitch">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            ⛓
          </span>
          <span className="brand-text">
            <strong>Workflow Orchestration Platform</strong>
            <small>Self-hosted job execution</small>
          </span>
        </div>
        <h1>Build, execute, monitor and retry reliable workflows.</h1>
        <p className="login-subtitle">
          Design a workflow as a graph, publish it, and let independent workers execute the steps with
          dependencies, retries and timeouts enforced by the platform.
        </p>
        <ul className="login-highlights">
          {HIGHLIGHTS.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </section>

      <section className="login-panel">
        <div className="login-card">
          <h2>Sign in</h2>
          <p className="login-card-subtitle">Use your Google account to continue.</p>

          {!supabaseConfigured ? (
            <div className="alert alert-warning" role="alert">
              <strong>Single sign-on is not configured.</strong>
              <ul>
                {configurationProblems.map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            </div>
          ) : null}

          {error ? (
            <div className="alert alert-error" role="alert">
              <span>{error}</span>
              <button type="button" className="alert-dismiss" onClick={clearError} aria-label="Dismiss">
                ×
              </button>
            </div>
          ) : null}

          <button
            type="button"
            className="button button-google"
            onClick={() => void signInWithGoogle()}
            disabled={busy || !supabaseConfigured}
          >
            {busy ? (
              <>
                <span className="spinner spinner-sm spinner-on-dark" aria-hidden="true" />
                {loading ? 'Checking your session…' : 'Redirecting to Google…'}
              </>
            ) : (
              <>
                <GoogleGlyph />
                Continue with Google
              </>
            )}
          </button>

          <p className="login-footnote">
            Your Google password is never shared with this application. We only receive your name,
            email address and profile picture.
          </p>
        </div>
      </section>
    </div>
  )
}

function GoogleGlyph() {
  return (
    <svg viewBox="0 0 18 18" width="18" height="18" aria-hidden="true" focusable="false">
      <path
        fill="#4285F4"
        d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.91c1.7-1.57 2.69-3.88 2.69-6.62Z"
      />
      <path
        fill="#34A853"
        d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.91-2.26c-.81.54-1.84.86-3.05.86-2.34 0-4.32-1.58-5.03-3.7H.96v2.33A9 9 0 0 0 9 18Z"
      />
      <path fill="#FBBC05" d="M3.97 10.72a5.41 5.41 0 0 1 0-3.44V4.95H.96a9 9 0 0 0 0 8.1l3.01-2.33Z" />
      <path
        fill="#EA4335"
        d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A9 9 0 0 0 .96 4.95l3.01 2.33C4.68 5.16 6.66 3.58 9 3.58Z"
      />
    </svg>
  )
}

export default LoginPage
