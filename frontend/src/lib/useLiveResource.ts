/**
 * A self-refreshing resource for live pages.
 *
 * Extends the plain `useResource` shape with:
 * - an interval tick that is skipped while the tab is hidden or the previous
 *   request is still in flight (never overlapping requests),
 * - `lastUpdatedAt` + `secondsAgo` for an "updated Xs ago" label,
 * - abort on unmount and on dependency change,
 * - a manual `refresh` that works even while polling is paused.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from './api'

export interface LiveResource<T> {
  data: T | null
  error: string | null
  loading: boolean
  initial: boolean
  refresh: () => void
  lastUpdatedAt: number | null
  secondsAgo: number | null
  hidden: boolean
}

export function useLiveResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  deps: unknown[],
  intervalMs: number,
  enabled = true,
): LiveResource<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null)
  const [secondsAgo, setSecondsAgo] = useState<number | null>(null)
  const [nonce, setNonce] = useState(0)
  const [hidden, setHidden] = useState(false)
  const loadedOnce = useRef(false)
  const inFlight = useRef(false)
  const loadRef = useRef(load)

  useEffect(() => {
    loadRef.current = load
  })

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    inFlight.current = true
    setLoading(true)
    loadRef.current(controller.signal)
      .then((value) => {
        if (!active) return
        setData(value)
        setError(null)
        setLastUpdatedAt(Date.now())
        loadedOnce.current = true
      })
      .catch((cause: unknown) => {
        if (!active || (cause instanceof DOMException && cause.name === 'AbortError')) return
        if (cause instanceof ApiError && cause.isAuthenticationFailure) {
          setError('Your session has ended. Please sign in again.')
          return
        }
        setError(cause instanceof Error ? cause.message : 'Something went wrong while loading this data.')
      })
      .finally(() => {
        if (!active) return
        inFlight.current = false
        setLoading(false)
      })

    return () => {
      active = false
      controller.abort()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])

  // Polling tick: only when enabled, visible and the previous request settled.
  // `nonce` is intentionally not a dependency — the interval must survive its
  // own ticks instead of restarting on every poll.
  useEffect(() => {
    if (!enabled || intervalMs <= 0) return
    const handle = window.setInterval(() => {
      if (document.hidden || inFlight.current) return
      setNonce((value) => value + 1)
    }, intervalMs)
    return () => window.clearInterval(handle)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, intervalMs, deps.length])

  // Track visibility so callers can show a paused indicator.
  useEffect(() => {
    const update = () => setHidden(document.hidden)
    update()
    document.addEventListener('visibilitychange', update)
    return () => document.removeEventListener('visibilitychange', update)
  }, [])

  // "Updated Xs ago" stays current between polls.
  useEffect(() => {
    if (lastUpdatedAt === null) return
    const update = () => setSecondsAgo(Math.max(0, Math.round((Date.now() - lastUpdatedAt) / 1000)))
    update()
    const handle = window.setInterval(update, 1000)
    return () => window.clearInterval(handle)
  }, [lastUpdatedAt])

  const refresh = useCallback(() => {
    if (inFlight.current) return
    setNonce((value) => value + 1)
  }, [])

  return { data, error, loading, initial: loading && !loadedOnce.current, refresh, lastUpdatedAt, secondsAgo, hidden }
}
