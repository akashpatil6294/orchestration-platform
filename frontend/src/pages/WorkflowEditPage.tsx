/**
 * Visual workflow builder: /workflows/:id/edit
 *
 * Canvas (React Flow) + task palette + inspector + problems panel + code view
 * + test panel + publish flow. Draft autosaves with If-Match optimistic
 * concurrency; conflicts surface a reload-or-overwrite dialog.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import { api, ApiError } from '../lib/api'
import CodeView from '../components/builder/CodeView'
import PublishModal from '../components/builder/PublishModal'
import SettingsDrawer from '../components/builder/SettingsDrawer'
import TestPanel from '../components/builder/TestPanel'
import {
  cloneDraft,
  edgesFromSteps,
  layoutSteps,
  newStepId,
  upstreamSteps,
  type BuilderDraft,
  type BuilderStep,
  type TaskTypeInfo,
  type ValidationIssue,
} from '../lib/builder'

interface WorkflowDetail {
  id: string
  name: string
  draft: BuilderDraft
  draft_version: number
  latest_version: number
}

type SaveState = 'idle' | 'saving' | 'saved' | 'conflict' | 'error'

export default function WorkflowEditPage() {
  const { workflowId } = useParams<{ workflowId: string }>()
  const navigate = useNavigate()
  const [workflow, setWorkflow] = useState<WorkflowDetail | null>(null)
  const [taskTypes, setTaskTypes] = useState<TaskTypeInfo[]>([])
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [saveError, setSaveError] = useState<string | null>(null)
  const [conflict, setConflict] = useState<{ current_version: number; current_draft: BuilderDraft } | null>(null)
  const [issues, setIssues] = useState<ValidationIssue[]>([])
  const [paletteQuery, setPaletteQuery] = useState('')
  const [showCode, setShowCode] = useState(false)
  const [showTest, setShowTest] = useState(false)
  const [showPublish, setShowPublish] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [pinnedOutputs, setPinnedOutputs] = useState<Record<string, unknown>>({})

  const draftRef = useRef<BuilderDraft | null>(null)
  const versionRef = useRef(1)
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const validateTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const undoStack = useRef<BuilderDraft[]>([])
  const redoStack = useRef<BuilderDraft[]>([])

  // --- Load -----------------------------------------------------------------
  useEffect(() => {
    if (!workflowId) return
    let cancelled = false
    Promise.all([
      api.get<WorkflowDetail>(`/api/v1/workflows/${workflowId}`),
      api.get<{ items: TaskTypeInfo[] }>('/api/v1/task-types'),
    ])
      .then(([wf, types]) => {
        if (cancelled) return
        setWorkflow(wf)
        setTaskTypes(types.items)
        draftRef.current = cloneDraft(wf.draft)
        versionRef.current = wf.draft_version
        syncCanvas(wf.draft)
      })
      .catch(() => {
        if (!cancelled) navigate('/workflows')
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflowId])

  // --- Canvas sync ------------------------------------------------------------
  const syncCanvas = useCallback(
    (draft: BuilderDraft) => {
      const steps = draft.steps ?? []
      const positions = layoutSteps(steps)
      setNodes(
        steps.map((s) => ({
          id: s.id,
          type: 'step',
          position: s.position ?? positions.get(s.id) ?? { x: 0, y: 0 },
          data: { label: s.id, taskType: s.type, issues: [] as ValidationIssue[] },
        })),
      )
      setEdges(
        edgesFromSteps(steps).map((e) => ({
          id: e.id,
          source: e.source,
          target: e.target,
          type: 'smoothstep',
        })),
      )
    },
    [setNodes, setEdges],
  )

  // --- Mutations (undoable) -----------------------------------------------------
  const pushUndo = useCallback(() => {
    if (draftRef.current) {
      undoStack.current.push(cloneDraft(draftRef.current))
      if (undoStack.current.length > 50) undoStack.current.shift()
      redoStack.current = []
    }
  }, [])

  const applyDraft = useCallback(
    (next: BuilderDraft, schedule = true) => {
      pushUndo()
      draftRef.current = next
      syncCanvas(next)
      if (schedule) scheduleSave()
      scheduleValidate()
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pushUndo, syncCanvas],
  )

  const undo = useCallback(() => {
    const prev = undoStack.current.pop()
    if (prev && draftRef.current) {
      redoStack.current.push(cloneDraft(draftRef.current))
      draftRef.current = prev
      syncCanvas(prev)
      scheduleSave()
      scheduleValidate()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [syncCanvas])

  const redo = useCallback(() => {
    const next = redoStack.current.pop()
    if (next && draftRef.current) {
      undoStack.current.push(cloneDraft(draftRef.current))
      draftRef.current = next
      syncCanvas(next)
      scheduleSave()
      scheduleValidate()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [syncCanvas])

  // --- Autosave -----------------------------------------------------------------
  const scheduleSave = useCallback(() => {
    if (saveTimer.current) clearTimeout(saveTimer.current)
    setSaveState('saving')
    saveTimer.current = setTimeout(() => void doSave(), 1200)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const doSave = useCallback(async () => {
    if (!workflowId || !draftRef.current) return
    try {
      const updated = await api.patchWithMatch<WorkflowDetail>(
        `/api/v1/workflows/${workflowId}`,
        { steps: draftRef.current.steps, name: draftRef.current.name, description: draftRef.current.description },
        versionRef.current,
      )
      versionRef.current = updated.draft_version
      setWorkflow((w) => (w ? { ...w, draft_version: updated.draft_version } : w))
      setSaveState('saved')
      setSaveError(null)
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        const details = (err.payload as { error?: { details?: { current_version: number; current_draft: BuilderDraft } } } | null)?.error?.details
        setSaveState('conflict')
        setConflict(details ?? null)
      } else {
        setSaveState('error')
        setSaveError(err instanceof Error ? err.message : 'Save failed')
      }
    }
  }, [workflowId])

  // --- Validation (debounced) -----------------------------------------------------
  const scheduleValidate = useCallback(() => {
    if (validateTimer.current) clearTimeout(validateTimer.current)
    validateTimer.current = setTimeout(() => void doValidate(), 400)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const doValidate = useCallback(async () => {
    if (!workflowId || !draftRef.current) return
    try {
      const result = await api.post<{ valid: boolean; errors: ValidationIssue[] }>(
        `/api/v1/workflows/${workflowId}/validate`,
        { definition: draftRef.current },
      )
      setIssues(result.errors ?? [])
      // Attach issues to nodes.
      setNodes((nds) =>
        nds.map((n) => ({
          ...n,
          data: { ...n.data, issues: (result.errors ?? []).filter((i) => i.step_id === n.id) },
        })),
      )
    } catch {
      // Validation failures must never break editing.
    }
  }, [workflowId, setNodes])

  // --- Canvas interactions --------------------------------------------------------
  const onConnect = useCallback(
    (connection: Connection) => {
      if (!draftRef.current || !connection.source || !connection.target) return
      const next = cloneDraft(draftRef.current)
      const step = next.steps.find((s) => s.id === connection.target)
      if (!step || (step.depends_on ?? []).includes(connection.source)) return
      step.depends_on = [...(step.depends_on ?? []), connection.source]
      applyDraft(next)
    },
    [applyDraft],
  )

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault()
      const taskType = event.dataTransfer.getData('application/x-task-type')
      if (!taskType || !draftRef.current) return
      const task = taskTypes.find((t) => t.task_type === taskType)
      const next = cloneDraft(draftRef.current)
      const id = newStepId(taskType.split('.').pop() ?? 'step')
      const step: BuilderStep = {
        id,
        type: taskType,
        input: {},
        depends_on: [],
        policy: task?.default_policy ? { ...task.default_policy } : {},
      }
      next.steps.push(step)
      applyDraft(next)
      setSelectedId(id)
    },
    [applyDraft, taskTypes],
  )

  const onNodeClick = useCallback((_: React.MouseEvent, node: Node) => {
    setSelectedId(node.id)
  }, [])

  const deleteSelected = useCallback(() => {
    if (!selectedId || !draftRef.current) return
    const next = cloneDraft(draftRef.current)
    next.steps = next.steps.filter((s) => s.id !== selectedId)
    next.steps.forEach((s) => {
      s.depends_on = (s.depends_on ?? []).filter((d) => d !== selectedId)
    })
    applyDraft(next)
    setSelectedId(null)
  }, [selectedId, applyDraft])

  const autoLayout = useCallback(() => {
    if (!draftRef.current) return
    const next = cloneDraft(draftRef.current)
    const positions = layoutSteps(next.steps)
    next.steps.forEach((s) => {
      s.position = positions.get(s.id)
    })
    // Bypass undo for layout (it's a view operation, not an edit).
    draftRef.current = next
    syncCanvas(next)
    scheduleSave()
  }, [syncCanvas, scheduleSave])

  // --- Keyboard ---------------------------------------------------------------------
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).tagName === 'INPUT' || (e.target as HTMLElement).tagName === 'TEXTAREA') return
      if ((e.ctrlKey || e.metaKey) && e.key === 'z' && !e.shiftKey) {
        e.preventDefault()
        undo()
      } else if ((e.ctrlKey || e.metaKey) && (e.key === 'y' || (e.key === 'z' && e.shiftKey))) {
        e.preventDefault()
        redo()
      } else if (e.key === 'Delete' || e.key === 'Backspace') {
        deleteSelected()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [undo, redo, deleteSelected])

  // --- Derived ------------------------------------------------------------------------
  const selectedStep = useMemo(
    () => draftRef.current?.steps.find((s) => s.id === selectedId) ?? null,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [selectedId, nodes],
  )
  const selectedTask = useMemo(
    () => taskTypes.find((t) => t.task_type === selectedStep?.type),
    [taskTypes, selectedStep],
  )
  const categories = useMemo(() => {
    const map = new Map<string, TaskTypeInfo[]>()
    for (const t of taskTypes) {
      if (paletteQuery && !`${t.task_type} ${t.description}`.toLowerCase().includes(paletteQuery.toLowerCase())) continue
      if (!map.has(t.category)) map.set(t.category, [])
      map.get(t.category)?.push(t)
    }
    return [...map.entries()]
  }, [taskTypes, paletteQuery])
  const errorCount = issues.filter((i) => !i.code.startsWith('graph.')).length

  if (!workflow) return <div className="builder-loading">Loading workflow…</div>

  return (
    <div className="builder" onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
      <header className="builder-toolbar">
        <button onClick={() => navigate(`/workflows/${workflowId}`)} className="btn-ghost">← Back</button>
        <h1>{draftRef.current?.name ?? workflow.name}</h1>
        <span className={`save-state save-${saveState}`} title={saveError ?? undefined}>
          {saveState === 'saving' && 'Saving…'}
          {saveState === 'saved' && 'Saved'}
          {saveState === 'idle' && 'Saved'}
          {saveState === 'conflict' && 'Conflict — resolve to continue'}
          {saveState === 'error' && `Save failed: ${saveError}`}
        </span>
        <div className="builder-actions">
          <button onClick={undo} disabled={undoStack.current.length === 0} title="Undo (Ctrl+Z)">Undo</button>
          <button onClick={redo} disabled={redoStack.current.length === 0} title="Redo (Ctrl+Y)">Redo</button>
          <button onClick={autoLayout} title="Auto-layout (keeps manual positions until clicked)">Layout</button>
          <button onClick={() => setShowCode((v) => !v)}>{showCode ? 'Canvas' : 'Code'}</button>
          <button onClick={() => setShowTest((v) => !v)}>Test</button>
          <button onClick={() => setShowSettings(true)}>Settings</button>
          <button onClick={() => setShowPublish(true)} disabled={errorCount > 0} title={errorCount > 0 ? `${errorCount} validation errors block publishing` : 'Publish'}>
            Publish{errorCount > 0 ? ` (${errorCount})` : ''}
          </button>
        </div>
      </header>

      <div className="builder-body">
        <aside className="builder-palette">
          <input
            placeholder="Search tasks…"
            value={paletteQuery}
            onChange={(e) => setPaletteQuery(e.target.value)}
            aria-label="Search task palette"
          />
          {categories.map(([category, tasks]) => (
            <section key={category}>
              <h3>{category}</h3>
              {tasks.map((t) => (
                <div
                  key={t.task_type}
                  className="palette-item"
                  draggable
                  onDragStart={(e) => e.dataTransfer.setData('application/x-task-type', t.task_type)}
                  title={t.description}
                >
                  <span className="palette-type">{t.task_type}</span>
                  <span className="palette-desc">{t.description}</span>
                </div>
              ))}
            </section>
          ))}
        </aside>

        <main className="builder-canvas">
          {showCode && draftRef.current ? (
            <CodeView
              draft={draftRef.current}
              onApply={(next) => applyDraft(next)}
            />
          ) : (
            <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodeClick={onNodeClick}
            snapToGrid
            snapGrid={[16, 16]}
            fitView
            minZoom={0.2}
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={16} />
            <Controls />
            <MiniMap />
          </ReactFlow>
          )}
        </main>

        <aside className="builder-inspector">
          {showTest && workflowId && draftRef.current ? (
            <TestPanel
              workflowId={workflowId}
              draft={draftRef.current}
              pinnedOutputs={pinnedOutputs}
              onPin={(stepId, output) => setPinnedOutputs((p) => ({ ...p, [stepId]: output }))}
            />
          ) : selectedStep && selectedTask ? (
            <Inspector
              step={selectedStep}
              task={selectedTask}
              steps={draftRef.current?.steps ?? []}
              issues={issues.filter((i) => i.step_id === selectedStep.id)}
              onChange={(patch) => {
                if (!draftRef.current) return
                const next = cloneDraft(draftRef.current)
                const target = next.steps.find((s) => s.id === selectedStep.id)
                if (target) Object.assign(target, patch)
                applyDraft(next)
              }}
            />
          ) : (
            <p className="inspector-empty">Select a step to edit its inputs, or drag a task from the palette.</p>
          )}
          <ProblemsPanel issues={issues} onSelect={setSelectedId} />
        </aside>
      </div>

      {conflict && (
        <ConflictDialog
          currentVersion={conflict.current_version}
          onReload={() => {
            draftRef.current = cloneDraft(conflict.current_draft)
            versionRef.current = conflict.current_version
            syncCanvas(conflict.current_draft)
            setConflict(null)
            setSaveState('saved')
          }}
          onOverwrite={async () => {
            versionRef.current = conflict.current_version
            setConflict(null)
            await doSave()
          }}
          onClose={() => setConflict(null)}
        />
      )}

      {showPublish && workflowId && workflow && (
        <PublishModal
          workflowId={workflowId}
          latestVersion={workflow.latest_version}
          issues={issues}
          onClose={() => setShowPublish(false)}
          onPublished={(version) => {
            setShowPublish(false)
            setWorkflow((w) => (w ? { ...w, latest_version: version } : w))
          }}
        />
      )}

      {showSettings && draftRef.current && (
        <SettingsDrawer
          draft={draftRef.current}
          onChange={(next) => applyDraft(next)}
          onClose={() => setShowSettings(false)}
        />
      )}
    </div>
  )
}

// --- Subcomponents (extracted below as the builder grows) -------------------------------

/** Canvas node: labelled station with status-aware border. */
function StepNode({ data, selected }: { data: { label: string; taskType: string; issues: ValidationIssue[] }; selected?: boolean }) {
  const hasIssues = data.issues.length > 0
  return (
    <div className={`step-node ${selected ? 'selected' : ''} ${hasIssues ? 'has-issues' : ''}`}>
      <Handle type="target" position={Position.Left} />
      <div className="step-node-id">{data.label}</div>
      <div className="step-node-type">{data.taskType}</div>
      {hasIssues && <div className="step-node-badge" title={data.issues.map((i) => i.message).join('\n')}>!</div>}
      <Handle type="source" position={Position.Right} />
    </div>
  )
}

const nodeTypes = { step: StepNode }

function Inspector(props: {
  step: BuilderStep
  task: TaskTypeInfo
  steps: BuilderStep[]
  issues: ValidationIssue[]
  onChange: (patch: Partial<BuilderStep>) => void
}) {
  const { step, task, steps, issues, onChange } = props
  const upstream = upstreamSteps(steps, step.id)
  const properties = task.input_schema.properties ?? {}

  const setInput = (key: string, value: unknown) => {
    onChange({ input: { ...(step.input ?? {}), [key]: value } })
  }

  return (
    <div className="inspector">
      <h2>{step.id}</h2>
      <p className="inspector-type">{task.task_type}</p>
      {task.description && <p className="inspector-desc">{task.description}</p>}

      {issues.length > 0 && (
        <ul className="inspector-issues">
          {issues.map((i, idx) => (
            <li key={idx} title={i.code}>{i.message}</li>
          ))}
        </ul>
      )}

      <section>
        <h3>Inputs</h3>
        {Object.entries(properties).map(([key, schema]) => (
          <SchemaField
            key={key}
            name={key}
            schema={schema}
            value={(step.input ?? {})[key]}
            upstream={upstream}
            issue={issues.find((i) => i.field === key || i.field === `input.${key}`)}
            onChange={(v) => setInput(key, v)}
          />
        ))}
        {Object.keys(properties).length === 0 && <p className="muted">No configurable inputs.</p>}
      </section>

      <section>
        <h3>Depends on</h3>
        <select
          multiple
          value={step.depends_on ?? []}
          onChange={(e) => {
            const selected = [...e.target.selectedOptions].map((o) => o.value)
            onChange({ depends_on: selected })
          }}
          aria-label="Depends on (keyboard-accessible edge editing)"
        >
          {steps.filter((s) => s.id !== step.id).map((s) => (
            <option key={s.id} value={s.id}>{s.id} ({s.type})</option>
          ))}
        </select>
        <p className="muted">Multi-select works with Ctrl/Cmd-click. Edges on the canvas stay in sync.</p>
      </section>

      <section>
        <h3>Policy</h3>
        <PolicyEditor step={step} task={task} onChange={onChange} />
      </section>
    </div>
  )
}

function SchemaField(props: {
  name: string
  schema: Record<string, unknown>
  value: unknown
  upstream: BuilderStep[]
  issue?: ValidationIssue
  onChange: (v: unknown) => void
}) {
  const { name, schema, value, upstream, issue, onChange } = props
  const [showAutocomplete, setShowAutocomplete] = useState(false)
  const schemaAny = schema as { type?: string; enum?: unknown[]; description?: string; default?: unknown }

  const insertReference = (path: string) => {
    const current = typeof value === 'string' ? value : ''
    onChange(`${current}{{${path}}}`)
    setShowAutocomplete(false)
  }

  const field = (() => {
    if (schemaAny.enum) {
      return (
        <select value={String(value ?? '')} onChange={(e) => onChange(e.target.value)}>
          <option value="">—</option>
          {schemaAny.enum.map((opt) => (
            <option key={String(opt)} value={String(opt)}>{String(opt)}</option>
          ))}
        </select>
      )
    }
    switch (schemaAny.type) {
      case 'boolean':
        return <input type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />
      case 'integer':
      case 'number':
        return (
          <input
            type="number"
            value={value === undefined ? '' : String(value)}
            onChange={(e) => onChange(e.target.value === '' ? undefined : Number(e.target.value))}
          />
        )
      case 'object':
      case 'array':
        return (
          <textarea
            rows={3}
            value={typeof value === 'string' ? value : JSON.stringify(value ?? '', null, 1)}
            onChange={(e) => {
              try {
                onChange(JSON.parse(e.target.value))
              } catch {
                onChange(e.target.value)
              }
            }}
          />
        )
      default:
        return (
          <div className="ref-field">
            <input
              type="text"
              value={typeof value === 'string' ? value : value === undefined ? '' : JSON.stringify(value)}
              placeholder={schemaAny.description ?? name}
              onChange={(e) => {
                onChange(e.target.value)
                setShowAutocomplete(e.target.value.endsWith('{{'))
              }}
            />
            {showAutocomplete && upstream.length > 0 && (
              <ul className="autocomplete" role="listbox">
                {upstream.map((s) => (
                  <li key={s.id}>
                    <button onClick={() => insertReference(`${s.id}.output`)}>{`{{${s.id}.output}}`}</button>
                    <span className="muted">{s.type}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )
    }
  })()

  return (
    <label className={`schema-field ${issue ? 'has-issue' : ''}`}>
      <span className="field-name">{name}</span>
      {field}
      {schemaAny.description && <span className="field-desc">{schemaAny.description}</span>}
      {issue && <span className="field-issue">{issue.message}</span>}
    </label>
  )
}

function PolicyEditor(props: { step: BuilderStep; task: TaskTypeInfo; onChange: (p: Partial<BuilderStep>) => void }) {
  const { step, task, onChange } = props
  const policy = (step.policy ?? {}) as Record<string, unknown>
  const set = (key: string, value: unknown) => onChange({ policy: { ...policy, [key]: value } })
  const retries = Number(policy.max_retries ?? 0)

  return (
    <div className="policy-editor">
      <label>Timeout (s)
        <input type="number" min={1} value={Number(policy.timeout_seconds ?? task.timeout_seconds)} onChange={(e) => set('timeout_seconds', Number(e.target.value))} />
      </label>
      <label>Max retries
        <input type="number" min={0} max={10} value={retries} onChange={(e) => set('max_retries', Number(e.target.value))} />
      </label>
      <label>Backoff
        <select value={String(policy.backoff ?? 'exponential')} onChange={(e) => set('backoff', e.target.value)}>
          <option value="fixed">fixed</option>
          <option value="exponential">exponential</option>
        </select>
      </label>
      {!task.idempotent && retries > 0 && (
        <p className="policy-warning" role="alert">
          ⚠ {task.task_type} is not idempotent — retries may repeat a side effect. Prefer max_retries 0 or make the step idempotent.
        </p>
      )}
      {task.idempotent && <p className="muted">✓ Safe to retry: this task is idempotent.</p>}
    </div>
  )
}

function ProblemsPanel(props: { issues: ValidationIssue[]; onSelect: (id: string) => void }) {
  const { issues, onSelect } = props
  if (issues.length === 0) return <p className="problems-empty">No validation issues.</p>
  return (
    <section className="problems-panel">
      <h3>Problems ({issues.length})</h3>
      <ul>
        {issues.map((issue, idx) => (
          <li key={idx}>
            <button onClick={() => issue.step_id && onSelect(issue.step_id)}>
              {issue.step_id ? `${issue.step_id}: ` : ''}{issue.message}
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}

function ConflictDialog(props: { currentVersion: number; onReload: () => void; onOverwrite: () => void; onClose: () => void }) {
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Save conflict">
      <div className="modal">
        <h2>Draft changed elsewhere</h2>
        <p>Another editor saved version {props.currentVersion} while you were editing. Your changes are still in the canvas.</p>
        <div className="modal-actions">
          <button onClick={props.onReload}>Reload their version (discard mine)</button>
          <button onClick={props.onOverwrite} className="btn-danger">Overwrite with mine</button>
          <button onClick={props.onClose} className="btn-ghost">Keep editing</button>
        </div>
      </div>
    </div>
  )
}
