/**
 * Public runtime configuration.
 *
 * Only `VITE_`-prefixed variables reach the browser, and only two secrets-free
 * values belong here: the Supabase project URL and its publishable (anon) key.
 * The Google client secret, the Supabase service-role key, the database URL and
 * the worker secrets stay on the server and must never appear in this bundle.
 */

const rawUrl = (import.meta.env.VITE_SUPABASE_URL ?? '').trim()
const rawAnonKey = (import.meta.env.VITE_SUPABASE_ANON_KEY ?? '').trim()

/** Where the FastAPI backend lives.
 *
 * Supports a relative base: when VITE_API_BASE_URL is empty (production
 * behind Caddy), the browser calls same-origin /api/* which Caddy proxies
 * to FastAPI. In dev it defaults to the local backend.
 */
export const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '')

export const supabaseUrl = rawUrl.replace(/\/+$/, '')
export const supabaseAnonKey = rawAnonKey

/**
 * True when Google sign-in can be attempted. When false the app renders a
 * configuration notice instead of a button that cannot work.
 */
export const supabaseConfigured = Boolean(supabaseUrl && supabaseAnonKey)

export const configurationProblems: string[] = [
  ...(supabaseUrl ? [] : ['VITE_SUPABASE_URL is not set.']),
  ...(supabaseAnonKey ? [] : ['VITE_SUPABASE_ANON_KEY is not set.']),
]

/** Key used to remember where a signed-out visitor was heading. */
export const REDIRECT_STORAGE_KEY = 'orchestrator.afterSignIn'
