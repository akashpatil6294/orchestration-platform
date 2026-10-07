import { useEffect, useRef, useState } from 'react'

import { api } from '../lib/api'

export interface StreamEvent {
  seq: number
  id: string
  type: string
  level: string
  message: string
  step_run_id: string | null
  step_key: string | null
  created_at: string | null
}

/** Live run events over SSE via fetch streaming (carries the Bearer token). */
export function useRunEventStream(runId: string, enabled: boolean, afterSeq: number) {
  const [events, setEvents] = useState<StreamEvent[]>([])
  const [connected, setConnected] = useState(false)
  const [done, setDone] = useState(false)
  const [latestSeq, setLatestSeq] = useState(afterSeq)
  const seqRef = useRef(afterSeq)

  useEffect(() => {
    if (!enabled || !runId) return
    let cancelled = false
    seqRef.current = Math.max(seqRef.current, afterSeq)
    setDone(false)
    setConnected(false)

    async function stream() {
      try {
        const response = await api.stream(`/api/v1/runs/${runId}/events/stream?after_seq=${seqRef.current}`, {
          headers: { Accept: 'text/event-stream' },
        })
        if (!response.ok || !response.body) return
        if (cancelled) return
        setConnected(true)
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        for (;;) {
          const { done: readerDone, value } = await reader.read()
          if (readerDone || cancelled) break
          buffer += decoder.decode(value, { stream: true })
          let index: number
          while ((index = buffer.indexOf('\n\n')) >= 0) {
            const frame = buffer.slice(0, index)
            buffer = buffer.slice(index + 2)
            if (frame.startsWith('event: done')) {
              setDone(true)
              return
            }
            for (const line of frame.split('\n')) {
              if (!line.startsWith('data: ')) continue
              try {
                const event = JSON.parse(line.slice(6)) as StreamEvent
                if (typeof event.seq === 'number') {
                  seqRef.current = Math.max(seqRef.current, event.seq)
                  setLatestSeq(seqRef.current)
                  setEvents((prev) => (prev.some((e) => e.seq === event.seq) ? prev : [...prev, event]))
                }
              } catch {
                // Ignore malformed frames.
              }
            }
          }
        }
      } catch {
        // Stream failed; the page keeps polling as a fallback.
      } finally {
        if (!cancelled) setConnected(false)
      }
    }

    stream()
    return () => {
      cancelled = true
      setConnected(false)
    }
  }, [runId, enabled, afterSeq])

  return { events, connected, done, latestSeq }
}
