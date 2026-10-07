import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { fakeSession, fakeSupabase } from '../test/fakeSupabase'
import { ApiError, api } from './api'

const fake = { current: fakeSupabase() }

vi.mock('./supabase', () => ({
  getSupabase: () => fake.current,
  resetSupabaseClient: () => {},
}))

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

describe('API client', () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    fake.current = fakeSupabase({ initialSession: fakeSession() })
    fetchMock.mockReset()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('attaches the Supabase access token to authenticated requests', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ items: [], total: 0 }))

    await api.get('/api/v1/workflows')

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('http://127.0.0.1:8000/api/v1/workflows')
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer fake-access-token')
  })

  it('sends anonymous requests without a token', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ status: 'ok' }))

    await api.anonymous('/health')

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined()
  })

  it('refreshes the session once and retries when the token has expired', async () => {
    const renewed = fakeSession({ email: 'renewed@example.com' })
    renewed.access_token = 'renewed-access-token'
    fake.current = fakeSupabase({ initialSession: fakeSession(), refreshSession: renewed })

    fetchMock
      .mockResolvedValueOnce(jsonResponse({ error: { code: 'invalid_credentials', message: 'expired' } }, 401))
      .mockResolvedValueOnce(jsonResponse({ items: [] }))

    await api.get('/api/v1/workflows')

    expect(fetchMock).toHaveBeenCalledTimes(2)
    const [, retry] = fetchMock.mock.calls[1] as [string, RequestInit]
    expect((retry.headers as Record<string, string>).Authorization).toBe('Bearer renewed-access-token')
  })

  it('clears the session and reports it when refresh cannot restore access', async () => {
    fake.current = fakeSupabase({ initialSession: fakeSession(), refreshSession: null })

    fetchMock.mockResolvedValue(jsonResponse({ error: { code: 'invalid_credentials', message: 'expired' } }, 401))

    await expect(api.get('/api/v1/workflows')).rejects.toMatchObject({ status: 401, code: 'session_expired' })
    expect(fake.current.auth.signOut).toHaveBeenCalled()
  })

  it('surfaces the backend error envelope', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { error: { code: 'email_in_use', message: 'An account with this email already exists.' } },
        409,
        { 'X-Request-Id': 'req-123' },
      ),
    )

    const error = await api.get('/api/v1/auth/session').catch((cause: unknown) => cause)
    expect(error).toBeInstanceOf(ApiError)
    expect(error as ApiError).toMatchObject({
      status: 409,
      code: 'email_in_use',
      message: 'An account with this email already exists.',
      requestId: 'req-123',
    })
  })

  it('serialises a JSON body and handles an empty response', async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }))

    const result = await api.post('/api/v1/workflows/abc/runs', { input: { env: 'prod' } })

    expect(result).toBeUndefined()
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(init.method).toBe('POST')
    expect(init.body).toBe(JSON.stringify({ input: { env: 'prod' } }))
  })
})
