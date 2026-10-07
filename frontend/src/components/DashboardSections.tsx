import { useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Link } from 'react-router-dom'
import type { DashboardResponse, DashboardStats, RunSummary, WorkerLoad } from '../lib/types'
import { formatDuration, formatRelative, StatusBadge } from './StatusView'
import { useToast } from './ToastProvider'

export function DashboardHeader({ children }: { children: ReactNode }) {
  const target = document.getElementById('dashboard-header')
  return target ? createPortal(children, target) : <div className="dashboard-header-fallback">{children}</div>
}

export function DeadLetterIndicator({ present }: { present: boolean }) {
  const target = document.getElementById('dead-letter-indicator')
  return present && target ? createPortal(<span className="status-square warn" aria-label="Dead letters need attention"/>, target) : null
}

export function AlertBar({ workers }: { workers: WorkerLoad[] }) {
  const [dismissed, setDismissed] = useState<string[]>([])
  const worker = workers.find(w => w.stale && !dismissed.includes(w.worker_id))
  if (!worker) return null
  return <aside className="worker-alert" aria-label="Stale worker">
    <span className="status-square warn" aria-hidden="true" />
    <span><strong className="mono">{worker.name || worker.worker_id}</strong> is stale · last heartbeat {formatRelative(worker.last_seen_at)}</span>
    <span aria-hidden="true">/</span><Link to="/ops/workers">Inspect worker →</Link>
    <button type="button" className="dismiss-alert" aria-label="Dismiss stale worker alert" onClick={() => setDismissed(ids => [...ids, worker.worker_id])}>×</button>
  </aside>
}

export function SummaryBand({ stats, range }: { stats: DashboardStats; range: string }) {
  return <section className="summary-band" aria-label="Execution summary">
    <Link to="/runs" className="summary-executions"><strong>{stats.runs_total.toLocaleString()}</strong><span>{stats.runs_total === 1 ? 'run' : 'runs'} · {Math.round(stats.success_rate * 100)}% success<br/><small>last {range}</small></span></Link>
    <Link to="/ops/workers" className="summary-cell"><span>Workers healthy</span><strong>{stats.active_workers}<small> / {stats.total_workers}</small></strong></Link>
    <Link to="/ops/workers" className="summary-cell"><span>Queue pending</span><strong>{stats.pending_steps}</strong></Link>
    <div className="summary-cell"><span>P95 latency</span><strong className="mono">{formatDuration(stats.p95_duration_seconds)}</strong></div>
    <Link to="/schedules" className="summary-cell"><span>Schedules active</span><strong>{stats.schedules_enabled}</strong></Link>
  </section>
}

export function RunsTable({ runs, renderActions }: { runs: RunSummary[]; renderActions: (run: RunSummary) => ReactNode }) {
  const [filter, setFilter] = useState('')
  const toast = useToast()
  const visible = runs.filter(run => `${run.workflow_name} ${run.workflow_id} ${run.id} ${run.status} v${run.version}`.toLowerCase().includes(filter.trim().toLowerCase()))
  async function copy(id: string) {
    try { await navigator.clipboard.writeText(id); toast.success('Run ID copied') }
    catch { toast.error('Could not copy run ID', 'Select the run ID and copy it manually.') }
  }
  return <section className="panel runs-panel" aria-label="Recent runs">
    <div className="panel-head"><h2>Recent runs</h2><input className="input run-filter" type="search" aria-label="Filter runs" placeholder="Filter runs…" value={filter} onChange={e => setFilter(e.target.value)}/></div>
    {visible.length ? <div className="table-scroll"><table className="table dashboard-runs-table"><thead><tr>
      {['Workflow', 'Run ID', 'Status', 'Steps', 'Duration', 'Started', 'Action'].map(label => <th scope="col" key={label}>{label}</th>)}
    </tr></thead><tbody>{visible.map(run => <tr key={run.id}>
      <td><Link to={`/workflows/${run.workflow_id}`}>{run.workflow_name || run.workflow_id}</Link><span className="row-subtitle mono">v{run.version}</span></td>
      <td><button className="copy-run mono" title={run.id} aria-label={`Copy run ID ${run.id}`} onClick={() => void copy(run.id)}><span>{run.id.slice(0, 8)}</span><svg className="copy-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="1"/><path d="M16 8V4H4v12h4"/></svg></button></td>
      <td><StatusBadge status={run.status}/></td><td className="mono">{run.completed_steps}/{run.total_steps}</td><td className="mono">{formatDuration(run.duration_seconds)}</td>
      <td className="mono" title={new Date(run.started_at || run.created_at).toLocaleString()}>{formatRelative(run.started_at || run.created_at)}</td>
      <td><Link className="trace-link" to={`/runs/${run.id}`}>View trace →</Link><details className="run-more"><summary>More actions</summary><div className="row-actions">{renderActions(run)}</div></details></td>
    </tr>)}</tbody></table></div> : <p className="panel-empty">{runs.length ? 'No runs match this filter. Try a workflow name, run ID, or status.' : 'No runs yet. Start a published workflow to see executions here.'}</p>}
    <footer className="panel-footer"><span>{visible.length} of {runs.length} recent runs</span><Link to="/runs">View all runs →</Link></footer>
  </section>
}

export function WorkersPanel({ workers, stats }: { workers: WorkerLoad[]; stats: DashboardStats }) {
  const used = workers.reduce((sum, w) => sum + w.running_steps, 0)
  const total = workers.reduce((sum, w) => sum + w.max_concurrency, 0)
  const occupied = total > 0 ? Math.min(16, Math.ceil(16 * used / total)) : 0
  const stale = workers.filter(w => w.stale).length
  return <section className="panel workers-panel" aria-label="Workers">
    <div className="panel-head"><h2>Workers</h2><span className="muted">{stats.total_workers} registered</span></div>
    <p className="worker-counts">{stats.active_workers} healthy · {stale} stale{workers.length < stats.total_workers ? ' in this list' : ''}</p>
    {total > 0 && <div className="slot-allocation"><div><span>Slot allocation{workers.length < stats.total_workers ? ' · shown workers' : ''}</span><span className="mono">{used}/{total}</span></div><div className="grid-cols-16" role="img" aria-label={`${used} of ${total} worker slots in use`}>{Array.from({length:16}, (_, i) => <span key={i} className={i < occupied ? 'slot-used' : ''}/>)}</div></div>}
    {workers.length ? <ul className="worker-list">{workers.map(w => <li key={w.worker_id} className={w.stale ? 'worker-stale' : ''}>
      <div className="worker-head"><span className={`status-square ${w.stale ? 'warn' : w.active ? 'ok' : 'idle'}`} aria-hidden="true"/><strong className="mono">{w.name || w.worker_id}</strong><span className="worker-status">{w.stale ? 'Stale' : w.active ? 'Healthy' : 'Idle'}</span></div>
      <p className="muted"><span className="mono">{w.running_steps}/{w.max_concurrency}</span> slots · heartbeat {formatRelative(w.last_seen_at)}</p>
    </li>)}</ul> : <p className="panel-empty">No workers connected. Register a worker in Settings to execute queued steps.</p>}
    <footer className="panel-footer"><Link to="/ops/workers">View all workers →</Link></footer>
  </section>
}

export function MetricsPanel({ data }: { data: DashboardResponse }) {
  const { stats } = data
  const buckets = data.runs_timeline ?? []
  const max = Math.max(1, ...buckets.map(b => b.started))
  const hasLatency = stats.avg_duration_seconds !== null || stats.p95_duration_seconds !== null
  return <section className="panel execution-metrics" aria-label="Execution metrics">
    <div className="panel-head"><h2>Execution metrics</h2><span className="muted">{stats.avg_duration_seconds !== null && <>Avg <span className="mono">{formatDuration(stats.avg_duration_seconds)}</span></>}{stats.p95_duration_seconds !== null && <> · P95 <span className="mono">{formatDuration(stats.p95_duration_seconds)}</span></>}</span></div>
    <div className={`metrics-grid${hasLatency ? '' : ' metrics-chart-only'}`}><div className="throughput">
      <h3>Runs started</h3>
      {stats.runs_total < 10 ? <p className="muted">Not enough runs yet. Charts appear after 10 runs.</p> : buckets.length === 0 ? <p className="muted">No time buckets available for this range.</p> : <div className="throughput-scroll"><div className="throughput-bars">{buckets.map(b => <div className="throughput-bucket" key={b.bucket_start}><div className="bar-track"><span className="throughput-bar" style={{height: `${b.started / max * 100}%`}} title={`${b.label}: ${b.started} runs`}/></div><span className="bucket-count">{b.started}</span><span className="bucket-label">{b.label}</span></div>)}</div></div>}
    </div>{hasLatency && <dl className="latency-list">{stats.avg_duration_seconds !== null && <div><dt>Average duration</dt><dd className="mono">{formatDuration(stats.avg_duration_seconds)}</dd></div>}{stats.p95_duration_seconds !== null && <div><dt>95th percentile</dt><dd className="mono">{formatDuration(stats.p95_duration_seconds)}</dd></div>}<div><dt>Finished runs</dt><dd>{stats.runs_finished}</dd></div><div><dt>Success rate</dt><dd>{Math.round(stats.success_rate * 100)}%</dd></div></dl>}</div>
  </section>
}
