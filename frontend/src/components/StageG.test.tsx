import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import RunTimeline from '../components/RunTimeline'
import WorkflowDag from '../components/WorkflowDag'

describe('RunTimeline', () => {
  it('renders a bar per timed step', () => {
    render(
      <RunTimeline
        steps={[
          { key: 'a', type: 'demo.echo', status: 'succeeded', started_at: '2026-10-06T10:00:00Z', finished_at: '2026-10-06T10:00:02Z', attempts: 1 },
          { key: 'b', type: 'demo.echo', status: 'failed', started_at: '2026-10-06T10:00:02Z', finished_at: '2026-10-06T10:00:05Z', attempts: 2 },
        ]}
      />,
    )
    expect(screen.getByText('a')).toBeTruthy()
    expect(screen.getByText('b')).toBeTruthy()
    expect(screen.getByText('2.0s')).toBeTruthy()
  })

  it('shows a placeholder when there is no timing data', () => {
    render(<RunTimeline steps={[]} />)
    expect(screen.getByText('No timing data yet.')).toBeTruthy()
  })
})

describe('WorkflowDag', () => {
  it('renders nodes for each step', () => {
    const { container } = render(
      <WorkflowDag
        steps={[
          { key: 'fetch', type: 'http.request', depends_on: [] },
          { key: 'parse', type: 'ai.extract', depends_on: ['fetch'] },
        ]}
      />,
    )
    expect(container.textContent).toContain('fetch')
    expect(container.textContent).toContain('parse')
  })

  it('shows a placeholder for empty definitions', () => {
    render(<WorkflowDag steps={[]} />)
    expect(screen.getByText('No steps.')).toBeTruthy()
  })
})
