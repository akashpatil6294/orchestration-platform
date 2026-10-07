/**
 * The Supabase browser client.
 *
 * Supabase is used for authentication only. It never reads or writes the
 * application's data: users, workflows, runs, schedules and workers all live in
 * the backend database and are reached through the FastAPI API.
 */
import { createClient, type SupabaseClient } from '@supabase/supabase-js'

import { supabaseAnonKey, supabaseConfigured, supabaseUrl } from './config'

let client: SupabaseClient | null = null

/**
 * Build (once) and return the shared client, or `null` when the project is not
 * configured. Returning `null` instead of throwing keeps an unconfigured build
 * from crashing the whole bundle.
 */
export function getSupabase(): SupabaseClient | null {
  if (!supabaseConfigured) return null
  if (client) return client
  client = createClient(supabaseUrl, supabaseAnonKey, {
    auth: {
      // Sessions are kept in localStorage and refreshed in the background, so a
      // page reload does not sign the user out.
      persistSession: true,
      autoRefreshToken: true,
      // Completes the Google round trip when Supabase redirects back to the app.
      detectSessionInUrl: true,
      // PKCE is the recommended flow for browser clients: the token exchange
      // happens over an HTTPS request instead of a URL fragment.
      flowType: 'pkce',
    },
  })
  return client
}

/** Test helper: forget the memoised client so a new configuration is picked up. */
export function resetSupabaseClient(): void {
  client = null
}
