/**
 * The authenticated API client.
 *
 * Every call to the backend goes through here so the bearer token is attached
 * in exactly one place. Nothing in the UI ever handles a raw token.
 *
 * Expiry handling: a `401` triggers one Supabase session refresh and one retry.
 * If the session cannot be restored the local session is cleared, which makes
 * the router send the visitor back to the login page.
 */
import { getSupabase } from './supabase'
import { apiBaseUrl } from './config'

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId: string | null
  readonly payload: unknown

  constructor(message: string, status: number, code: string, requestId: string | null = null, payload: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.requestId = requestId
    this.payload = payload
  }

  /** The session is gone and the visitor has to sign in again. */
  get isAuthenticationFailure(): boolean {
    return this.status === 401
  }
}

type Body = unknown

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: Body
  /** Anonymous calls (the health endpoint) skip the token entirely. */
  authenticated?: boolean
  signal?: AbortSignal
  headers?: Record<string, string>
}

async function accessToken(): Promise<string | null> {
  const supabase = getSupabase()
  if (!supabase) return null
  const { data, error } = await supabase.auth.getSession()
  if (error) return null
  return data.session?.access_token ?? null
}

async function refreshAccessToken(): Promise<string | null> {
  const supabase = getSupabase()
  if (!supabase) return null
  const { data, error } = await supabase.auth.refreshSession()
  if (error) return null
  return data.session?.access_token ?? null
}

async function abandonSession(): Promise<void> {
  const supabase = getSupabase()
  if (!supabase) return
  try {
    await supabase.auth.signOut()
  } catch {
    // Already signed out server-side; the local session is what matters.
  }
}

function messageFrom(payload: unknown, status: number): { message: string; code: string } {
  if (payload && typeof payload === 'object') {
    const envelope = (payload as { error?: unknown }).error
    if (envelope && typeof envelope === 'object') {
      const { message, code } = envelope as { message?: unknown; code?: unknown }
      return {
        message: typeof message === 'string' && message ? message : `Request failed (${status})`,
        code: typeof code === 'string' && code ? code : 'request_failed',
      }
    }
    const detail = (payload as { detail?: unknown }).detail
    if (typeof detail === 'string' && detail) return { message: detail, code: 'request_failed' }
  }
  return { message: `Request failed (${status})`, code: 'request_failed' }
}

async function send(path: string, options: RequestOptions, token: string | null): Promise<Response> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  Object.assign(headers, options.headers ?? {})

  return fetch(`${apiBaseUrl}${path}`, {
    method: options.method ?? 'GET',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  })
}

/** Authenticated raw fetch for streaming endpoints; returns the Response untouched. */
async function streamRequest(path: string, init: RequestInit = {}): Promise<Response> {
  const token = await accessToken()
  const headers = new Headers(init.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  return fetch(`${apiBaseUrl}${path}`, { ...init, headers })
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const authenticated = options.authenticated ?? true
  const token = authenticated ? await accessToken() : null

  let response = await send(path, options, token)

  if (response.status === 401 && authenticated) {
    const refreshed = await refreshAccessToken()
    if (refreshed) {
      response = await send(path, options, refreshed)
    }
    if (response.status === 401) {
      await abandonSession()
      throw new ApiError('Your session has ended. Please sign in again.', 401, 'session_expired')
    }
  }

  if (response.status === 204) {
    return undefined as T
  }

  const text = await response.text()
  let payload: unknown = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = text
    }
  }

  if (!response.ok) {
    const { message, code } = messageFrom(payload, response.status)
    throw new ApiError(message, response.status, code, response.headers.get('X-Request-Id'), payload)
  }

  return payload as T
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) => apiRequest<T>(path, { signal }),
  post: <T>(path: string, body?: Body) => apiRequest<T>(path, { method: 'POST', body }),
  patch: <T>(path: string, body?: Body) => apiRequest<T>(path, { method: 'PATCH', body }),
  /** PATCH with an If-Match header for optimistic concurrency (draft_version). */
  patchWithMatch: <T>(path: string, body: Body, version: number | string) =>
    apiRequest<T>(path, {
      method: 'PATCH',
      body,
      headers: { 'If-Match': String(version) },
    }),
  put: <T>(path: string, body?: Body) => apiRequest<T>(path, { method: 'PUT', body }),
  remove: <T>(path: string) => apiRequest<T>(path, { method: 'DELETE' }),
  anonymous: <T>(path: string) => apiRequest<T>(path, { authenticated: false }),
  /** Raw authenticated response for streaming endpoints (e.g. SSE). */
  stream: (path: string, init?: RequestInit) => streamRequest(path, init),
}

export interface DocumentInfo {
  id: string
  filename: string
  content_type: string
  size_bytes: number
  sha256: string
  created_at: string
}

/** Authenticated binary download (e.g. a stored document). */
export async function downloadBlob(path: string): Promise<Blob> {
  const token = await accessToken()
  const headers: Record<string, string> = {}
  if (token) headers.Authorization = `Bearer ${token}`
  const response = await fetch(`${apiBaseUrl}${path}`, { headers })
  if (!response.ok) {
    const text = await response.text()
    let payload: unknown = null
    try {
      payload = text ? JSON.parse(text) : null
    } catch {
      payload = text
    }
    const { message, code } = messageFrom(payload, response.status)
    throw new ApiError(message, response.status, code, response.headers.get('X-Request-Id'))
  }
  return response.blob()
}

/** Multipart upload with progress. Streams the file; the server enforces the
 *  size limit while reading and rejects over-limit uploads with 413. */
export function uploadDocument(file: File, onProgress: (fraction: number) => void): Promise<DocumentInfo> {
  return new Promise((resolve, reject) => {
    const run = async () => {
      const token = await accessToken()
      const xhr = new XMLHttpRequest()
      xhr.open('POST', `${apiBaseUrl}/api/v1/documents`)
      if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress(event.loaded / event.total)
      }
      xhr.onload = () => {
        let payload: unknown = null
        try {
          payload = xhr.responseText ? JSON.parse(xhr.responseText) : null
        } catch {
          payload = xhr.responseText
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(payload as DocumentInfo)
        } else {
          const { message, code } = messageFrom(payload, xhr.status)
          reject(new ApiError(message, xhr.status, code, xhr.getResponseHeader('X-Request-Id')))
        }
      }
      xhr.onerror = () => reject(new ApiError('The upload could not reach the server.', 0, 'network_error'))
      xhr.onabort = () => reject(new ApiError('The upload was cancelled.', 0, 'upload_cancelled'))
      const form = new FormData()
      form.append('file', file, file.name)
      xhr.send(form)
    }
    run().catch(reject)
  })
}
