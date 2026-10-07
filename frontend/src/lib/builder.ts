/**
 * Visual builder core: types, layout, and editing helpers for /workflows/:id/edit.
 *
 * The canvas edits a Draft (steps + workflow-level fields). Every mutation
 * goes through the EditorState so undo/redo and autosave stay consistent.
 */

export interface TaskTypeInfo {
  task_type: string
  description: string
  timeout_seconds: number
  side_effects: boolean
  tags: string[]
  input_schema: JsonSchema
  output_schema: JsonSchema | null
  category: string
  icon_key: string
  idempotent: boolean
  default_policy: Record<string, unknown>
}

export interface JsonSchema {
  type?: string
  properties?: Record<string, JsonSchema & { description?: string; default?: unknown; enum?: unknown[] }>
  required?: string[]
  items?: JsonSchema
  description?: string
  [key: string]: unknown
}

export interface BuilderStep {
  id: string
  type: string
  input?: Record<string, unknown>
  depends_on?: string[]
  policy?: Record<string, unknown>
  position?: { x: number; y: number }
  [key: string]: unknown
}

export interface BuilderDraft {
  name: string
  description?: string
  timeout_seconds?: number
  sla_seconds?: number
  default_max_parallel?: number
  default_queue?: string
  on_failure?: BuilderStep[]
  steps: BuilderStep[]
  [key: string]: unknown
}

export interface ValidationIssue {
  code: string
  message: string
  step_id: string | null
  field: string | null
}

/** Deterministic layered layout: depth = longest path from a root. Never moves manually-placed nodes. */
export function layoutSteps(
  steps: BuilderStep[],
  opts: { xGap?: number; yGap?: number } = {},
): Map<string, { x: number; y: number }> {
  const xGap = opts.xGap ?? 240
  const yGap = opts.yGap ?? 100
  const byId = new Map(steps.map((s) => [s.id, s]))
  const depth = new Map<string, number>()
  function compute(id: string, seen: Set<string>): number {
    if (depth.has(id)) return depth.get(id) as number
    if (seen.has(id)) return 0 // cycle: break gracefully
    seen.add(id)
    const step = byId.get(id)
    const deps = step?.depends_on ?? []
    const d = deps.length > 0 ? 1 + Math.max(...deps.map((p) => compute(p, seen))) : 0
    depth.set(id, d)
    return d
  }
  steps.forEach((s) => compute(s.id, new Set()))
  const layers = new Map<number, string[]>()
  steps.forEach((s) => {
    const d = depth.get(s.id) ?? 0
    if (!layers.has(d)) layers.set(d, [])
    layers.get(d)?.push(s.id)
  })
  const positions = new Map<string, { x: number; y: number }>()
  layers.forEach((ids, d) => {
    ids.forEach((id, i) => {
      positions.set(id, { x: d * xGap, y: i * yGap })
    })
  })
  return positions
}

/** Build React Flow edges from depends_on lists. */
export function edgesFromSteps(steps: BuilderStep[]): { id: string; source: string; target: string }[] {
  const edges: { id: string; source: string; target: string }[] = []
  const ids = new Set(steps.map((s) => s.id))
  for (const step of steps) {
    for (const dep of step.depends_on ?? []) {
      if (ids.has(dep)) edges.push({ id: `${dep}->${step.id}`, source: dep, target: step.id })
    }
  }
  return edges
}

/** Find upstream steps (transitive dependencies) for the {{ autocomplete. */
export function upstreamSteps(steps: BuilderStep[], stepId: string): BuilderStep[] {
  const byId = new Map(steps.map((s) => [s.id, s]))
  const seen = new Set<string>()
  const result: BuilderStep[] = []
  function visit(id: string): void {
    const step = byId.get(id)
    if (!step) return
    for (const dep of step.depends_on ?? []) {
      if (!seen.has(dep)) {
        seen.add(dep)
        const depStep = byId.get(dep)
        if (depStep) result.push(depStep)
        visit(dep)
      }
    }
  }
  visit(stepId)
  return result
}

/** Known output paths for a task type, from its output_schema. */
export function outputPaths(task: TaskTypeInfo | undefined): string[] {
  const props = task?.output_schema?.properties
  if (!props) return []
  return Object.keys(props)
}

let stepCounter = 0
export function newStepId(prefix = 'step'): string {
  stepCounter += 1
  return `${prefix}_${Date.now().toString(36)}_${stepCounter}`
}

/** Deep-clone a draft for undo snapshots. */
export function cloneDraft(draft: BuilderDraft): BuilderDraft {
  return JSON.parse(JSON.stringify(draft))
}
