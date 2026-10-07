/**
 * Minimal data-loading primitive.
 *
 * Keeps the loading / error / empty triad consistent across pages without
 * pulling in a data-fetching library. Every endpoint here is a plain request
 * that is re-run on demand, which is all the polling UI needs.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from './api'

export interface Resource<T> {
  data: T | null
  error: string | null
  loading: boolean
  /** True only for the very first load, so refreshing does not blank the page. */
  initial: boolean
  reload: () => void
  setData: (value: T | null) => void
}

export function useResource<T>(load: (signal: AbortSignal) => Promise<T>, deps: unknown[]): Resource<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [nonce, setNonce] = useState(0)
  const loadedOnce = useRef(false)
  const loadRef = useRef(load)

  // Refreshed in an effect, never during render, and declared before the fetch
  // effect so the newest loader is always the one that runs.
  useEffect(() => {
    loadRef.current = load
  })

  useEffect(() => {
    const controller = new AbortController()
    let active = true

    setLoading(true)
    loadRef
      .current(controller.signal)
      .then((value) => {
        if (!active) return
        setData(value)
        setError(null)
      })
      .catch((cause: unknown) => {
        if (!active || (cause instanceof DOMException && cause.name === 'AbortError')) return
        if (cause instanceof ApiError && cause.isAuthenticationFailure) {
          // The router reacts to the cleared session; keep the message brief.
          setError('Your session has ended. Please sign in again.')
          return
        }
        setError(cause instanceof Error ? cause.message : 'Something went wrong while loading this data.')
      })
      .finally(() => {
        if (!active) return
        setLoading(false)
        loadedOnce.current = true
      })

    return () => {
      active = false
      controller.abort()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])

  const reload = useCallback(() => setNonce((value) => value + 1), [])

  return { data, error, loading, initial: loading && !loadedOnce.current, reload, setData }
}

/**
 * Re-runs `tick` on an interval while `enabled`, for live run progress. Polling
 * is deliberate for now; the shape of this hook is what a later switch to
 * server-sent events or WebSockets would replace.
 */
export function usePolling(tick: () => void, intervalMs: number, enabled = true): void {
  const tickRef = useRef(tick)

  useEffect(() => {
    tickRef.current = tick
  })

  useEffect(() => {
    if (!enabled) return
    const handle = window.setInterval(() => tickRef.current(), intervalMs)
    return () => window.clearInterval(handle)
  }, [intervalMs, enabled])
}
