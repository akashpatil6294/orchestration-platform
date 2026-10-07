/** Visual builder core helpers (Stage H, H2). */
import { describe, expect, it } from 'vitest'
import {
  cloneDraft,
  edgesFromSteps,
  layoutSteps,
  outputPaths,
  upstreamSteps,
  type BuilderDraft,
  type TaskTypeInfo,
} from '../lib/builder'

const draft: BuilderDraft = {
  name: 'test',
  steps: [
    { id: 'a', type: 'demo.echo' },
    { id: 'b', type: 'demo.echo', depends_on: ['a'] },
    { id: 'c', type: 'demo.echo', depends_on: ['a'] },
    { id: 'd', type: 'demo.echo', depends_on: ['b', 'c'] },
  ],
}

describe('layoutSteps', () => {
  it('assigns increasing x by dependency depth', () => {
    const positions = layoutSteps(draft.steps)
    const x = (id: string) => positions.get(id)?.x ?? -1
    expect(x('a')).toBeLessThan(x('b'))
    expect(x('b')).toBeLessThan(x('d'))
    expect(x('a')).toBeLessThan(x('c'))
  })

  it('places siblings on different rows', () => {
    const positions = layoutSteps(draft.steps)
    expect(positions.get('b')?.y).not.toBe(positions.get('c')?.y)
  })

  it('does not throw on cycles', () => {
    const cyclic: BuilderDraft = {
      name: 'cyclic',
      steps: [
        { id: 'a', type: 'demo.echo', depends_on: ['b'] },
        { id: 'b', type: 'demo.echo', depends_on: ['a'] },
      ],
    }
    expect(() => layoutSteps(cyclic.steps)).not.toThrow()
  })
})

describe('edgesFromSteps', () => {
  it('creates one edge per dependency', () => {
    const edges = edgesFromSteps(draft.steps)
    expect(edges).toHaveLength(4)
    expect(edges.map((e) => e.id)).toContain('a->b')
  })

  it('ignores dangling dependencies', () => {
    const edges = edgesFromSteps([{ id: 'a', type: 'demo.echo', depends_on: ['ghost'] }])
    expect(edges).toHaveLength(0)
  })
})

describe('upstreamSteps', () => {
  it('returns transitive dependencies', () => {
    const upstream = upstreamSteps(draft.steps, 'd').map((s) => s.id)
    expect(upstream).toContain('a')
    expect(upstream).toContain('b')
    expect(upstream).toContain('c')
    expect(upstream).not.toContain('d')
  })
})

describe('outputPaths', () => {
  it('lists output schema properties', () => {
    const task = {
      output_schema: { type: 'object', properties: { summary: { type: 'string' }, confidence: { type: 'number' } } },
    } as unknown as TaskTypeInfo
    expect(outputPaths(task)).toEqual(['summary', 'confidence'])
  })

  it('returns empty when no schema', () => {
    expect(outputPaths(undefined)).toEqual([])
    expect(outputPaths({ output_schema: null } as TaskTypeInfo)).toEqual([])
  })
})

describe('cloneDraft', () => {
  it('deep-clones', () => {
    const copy = cloneDraft(draft)
    copy.steps[0].id = 'changed'
    expect(draft.steps[0].id).toBe('a')
  })
})
