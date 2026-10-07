import { useMemo } from 'react'
import { Background, Controls, MiniMap, ReactFlow, type Edge, type Node } from '@xyflow/react'
import '@xyflow/react/dist/style.css'

export interface DagStep {
  key: string
  type: string
  status?: string
  depends_on: string[]
}

const STATUS_BORDER: Record<string, string> = {
  succeeded: '#2f9e44',
  failed: '#e03131',
  running: '#1971c2',
  retrying: '#f08c00',
  pending: '#868e96',
  cancelled: '#868e96',
  skipped: '#868e96',
}

function layoutSteps(steps: DagStep[]): { nodes: Node[]; edges: Edge[] } {
  // Simple layered layout: depth = longest path from a root.
  const depth = new Map<string, number>()
  const byKey = new Map(steps.map((s) => [s.key, s]))
  function compute(key: string, seen: Set<string>): number {
    if (depth.has(key)) return depth.get(key) as number
    if (seen.has(key)) return 0
    seen.add(key)
    const step = byKey.get(key)
    const d = step && step.depends_on.length > 0 ? 1 + Math.max(...step.depends_on.map((p) => compute(p, seen))) : 0
    depth.set(key, d)
    return d
  }
  steps.forEach((s) => compute(s.key, new Set()))
  const layers = new Map<number, string[]>()
  steps.forEach((s) => {
    const d = depth.get(s.key) ?? 0
    if (!layers.has(d)) layers.set(d, [])
    layers.get(d)?.push(s.key)
  })
  const nodes: Node[] = []
  layers.forEach((keys, d) => {
    keys.forEach((key, i) => {
      const step = byKey.get(key) as DagStep
      nodes.push({
        id: key,
        position: { x: d * 220, y: i * 90 },
        data: {
          label: `${key}\n${step.type}${step.status ? ` · ${step.status}` : ''}`,
        },
        style: {
          border: `2px solid ${STATUS_BORDER[step.status ?? ''] ?? '#adb5bd'}`,
          borderRadius: 8,
          padding: 10,
          fontSize: 12,
          whiteSpace: 'pre-line',
          background: 'var(--surface)',
          minWidth: 160,
        },
      })
    })
  })
  const edges: Edge[] = []
  steps.forEach((s) => {
    s.depends_on.forEach((parent) => {
      if (byKey.has(parent)) edges.push({ id: `${parent}->${s.key}`, source: parent, target: s.key })
    })
  })
  return { nodes, edges }
}

/** Read-only DAG visualization of a workflow definition or a run's steps. */
export default function WorkflowDag({ steps, onSelect }: { steps: DagStep[]; onSelect?: (key: string) => void }) {
  const { nodes, edges } = useMemo(() => layoutSteps(steps), [steps])
  if (steps.length === 0) return <p className="muted">No steps.</p>
  return (
    <div style={{ height: 420 }} className="dag-canvas">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        onNodeClick={(_, node) => onSelect?.(node.id)}
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls />
        <MiniMap />
      </ReactFlow>
    </div>
  )
}
