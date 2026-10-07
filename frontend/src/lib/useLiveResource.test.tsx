import { act, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useLiveResource } from './useLiveResource'

const apiGet = vi.hoisted(() => vi.fn())
vi.mock('./api', () => ({
  ApiError: class ApiError extends Error {},
  api: { get: apiGet },
}))

function Harness({ intervalMs }: { intervalMs: number }) {
  const resource = useLiveResource<{ value: number }>(() => apiGet(), [], intervalMs, intervalMs > 0)
  return (
    <div>
      <span data-testid="value">{resource.data?.value ?? 'none'}</span>
      <span data-testid="loading">{String(resource.loading)}</span>
      <span data-testid="seconds-ago">{resource.secondsAgo ?? '—'}</span>
      <button type="button" onClick={resource.refresh}>
        refresh
      </button>
    </div>
  )
}

describe('useLiveResource', () => {
  beforeEach(() => {
    apiGet.mockReset()
    apiGet.mockResolvedValue({ value: 1 })
  })

  it('loads once on mount', async () => {
    render(<Harness intervalMs={0} />)
    await waitFor(() => expect(screen.getByTestId('value')).toHaveTextContent('1'))
    expect(apiGet).toHaveBeenCalledTimes(1)
  })

  it('polls on the interval without overlapping requests', async () => {
    vi.useFakeTimers()
    // A request that is still in flight must not trigger a second one.
    let release!: (value: { value: number }) => void
    apiGet.mockImplementationOnce(() => new Promise<{ value: number }>((resolve) => (release = resolve)))
    render(<Harness intervalMs={1000} />)
    await act(async () => {
      release({ value: 42 })
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByTestId('value')).toHaveTextContent('42')

    // Advance one interval at a time so React settles between ticks.
    for (let step = 0; step < 4; step += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000)
      })
    }
    const calls = apiGet.mock.calls.length
    expect(calls).toBeGreaterThanOrEqual(4)
    expect(calls).toBeLessThanOrEqual(5) // initial + one per tick; never overlapping
    vi.useRealTimers()
  })

  it('tracks seconds since the last successful update', async () => {
    vi.useFakeTimers()
    render(<Harness intervalMs={0} />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(Number(screen.getByTestId('seconds-ago').textContent)).toBeGreaterThanOrEqual(3)
    vi.useRealTimers()
  })
})
