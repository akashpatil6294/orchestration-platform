import { writeFileSync } from 'node:fs'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DashboardResponse, RunSummary } from '../lib/types'
import { DashboardPage } from '../pages/DashboardPage'
import { AppShell } from './AppShell'

const apiGet = vi.hoisted(() => vi.fn())
vi.mock('../lib/api', () => ({ ApiError: class extends Error {}, api: { get: apiGet, post: vi.fn() } }))
vi.mock('../auth/AuthProvider', () => ({
  useAuth: () => ({ user: { id: 'test-user', email: 'review@example.test' }, signOut: vi.fn() }),
  displayNameFor: () => 'Layout review', avatarUrlFor: () => null, initialsFor: () => 'LR',
}))
const now = new Date().toISOString()
const run: RunSummary = { id: 'run-review-001', workflow_id: 'workflow-review', workflow_name: 'Test workflow', version: 2, status: 'succeeded', trigger: 'manual', created_at: now, started_at: now, finished_at: now, duration_seconds: 2, step_counts: { succeeded: 4 }, total_steps: 4, completed_steps: 4, progress: 1, cancel_requested: false }
function response(): DashboardResponse {
  return {
    stats: { window_hours: 24, runs_total: 12, runs_by_status: [{status:'succeeded',count:11},{status:'failed',count:1}], runs_started: 12, runs_finished: 12, success_rate: 11/12, avg_duration_seconds: 2, p95_duration_seconds: 4, step_attempts: 48, retried_steps: 1, failed_steps: 1, timed_out_steps: 0, active_workers: 1, total_workers: 2, pending_steps: 0, running_steps: 2, schedules_enabled: 1, schedules_due: 0, workflows_total: 1, latest_event_at: now },
    recent_runs: [run, {...run, id:'run-review-002', status:'failed', completed_steps:3, step_counts:{succeeded:3,failed:1}}], recent_activity: [], top_workflows: [], needs_attention: [],
    workers: [{worker_id:'worker-1',name:'Test worker',active:true,stale:false,task_types:['demo.echo'],max_concurrency:4,running_steps:2,last_seen_at:now}, {worker_id:'worker-2',name:'Stale test worker',active:false,stale:true,task_types:['demo.echo'],max_concurrency:4,running_steps:0,last_seen_at:new Date(Date.now()-120000).toISOString()}],
    runs_timeline: [{bucket_start:'2026-10-07T00:00:00Z',label:'00:00',started:3,succeeded:3,failed:0},{bucket_start:'2026-10-07T08:00:00Z',label:'08:00',started:4,succeeded:4,failed:0},{bucket_start:'2026-10-07T16:00:00Z',label:'16:00',started:5,succeeded:4,failed:1}], duration_trend: [],
  }
}
function mount() { return render(<MemoryRouter initialEntries={['/dashboard']}><Routes><Route element={<AppShell/>}><Route path="/dashboard" element={<DashboardPage/>}/></Route></Routes></MemoryRouter>) }
beforeEach(() => {
  localStorage.clear()
  apiGet.mockReset()
  apiGet.mockImplementation((path: string) => Promise.resolve(path.startsWith('/api/v1/runs/dashboard') ? response() : {items:[]}))
})
describe('Dashboard presentation', () => {
  it('filters loaded runs and retains the trace destination', async () => {
    const user = userEvent.setup(); mount()
    const filter = await screen.findByRole('searchbox', {name:'Filter runs'})
    await user.type(filter, 'run-review-002')
    expect(screen.getAllByRole('link', {name:'View trace →'})).toHaveLength(1)
    expect(screen.getByRole('link', {name:'View trace →'})).toHaveAttribute('href','/runs/run-review-002')
    await user.clear(filter); await user.type(filter,'no-match')
    expect(screen.getByText('No runs match this filter. Try a workflow name, run ID, or status.')).toBeInTheDocument()
  })
  it('changes the existing query when selecting a time range', async () => {
    const user = userEvent.setup(); mount()
    const control = await screen.findByRole('group',{name:'Time window'})
    await user.click(within(control).getByRole('button',{name:'7d'}))
    await vi.waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/v1/runs/dashboard?window_hours=168', expect.any(AbortSignal)))
    expect(screen.getByText('last 7d')).toBeInTheDocument()
  })
  it('dismisses a real stale worker and computes slots from capacities', async () => {
    const user=userEvent.setup(); mount()
    expect(await screen.findByRole('img',{name:'2 of 8 worker slots in use'})).toBeInTheDocument()
    await user.click(screen.getByRole('button',{name:'Dismiss stale worker alert'}))
    expect(screen.queryByRole('complementary',{name:'Stale worker'})).not.toBeInTheDocument()
  })
  it('copies the full run ID without navigating', async () => {
    const user=userEvent.setup(); mount()
    await user.click(await screen.findByRole('button',{name:'Copy run ID run-review-001'}))
    expect(await navigator.clipboard.readText()).toBe('run-review-001')
    expect(await screen.findByText('Run ID copied')).toBeInTheDocument()
  })
  it('renders no invented latency or throughput on an empty dashboard', async () => {
    const empty=response(); empty.stats.runs_total=0; empty.stats.avg_duration_seconds=null; empty.stats.p95_duration_seconds=null; empty.workers=[]; empty.recent_runs=[]; empty.runs_timeline=[]
    apiGet.mockImplementation((path:string)=>Promise.resolve(path.startsWith('/api/v1/runs/dashboard')?empty:{items:[]}))
    mount()
    expect(await screen.findByText('Not enough runs yet. Charts appear after 10 runs.')).toBeInTheDocument()
    expect(screen.queryByText('95th percentile')).not.toBeInTheDocument()
    expect(screen.queryByRole('complementary',{name:'Stale worker'})).not.toBeInTheDocument()
  })
  it('shows the dead-letter dot only when the existing response reports dead letters', async () => {
    const body = response()
    body.needs_attention = [{kind:'dlq',id:'dlq',run_id:null,workflow_id:null,severity:'bad',label:'Dead letters',detail:'1 failed step waiting for redrive',at:now}]
    apiGet.mockImplementation((path:string)=>Promise.resolve(path.startsWith('/api/v1/runs/dashboard')?body:{items:[]}))
    mount()
    expect(await screen.findByLabelText('Dead letters need attention')).toBeInTheDocument()
  })
  it('opens and closes the mobile navigation with keyboard support', async () => {
    const user=userEvent.setup(); mount()
    const toggle=screen.getByRole('button',{name:'Open navigation'})
    await user.click(toggle)
    expect(screen.getByRole('button',{name:'Close navigation'})).toHaveAttribute('aria-expanded','true')
    screen.getByRole('link',{name:'Dashboard'}).focus()
    await user.keyboard('{Escape}')
    expect(screen.getByRole('button',{name:'Open navigation'})).toHaveAttribute('aria-expanded','false')
  })
  it('renders the dashboard inside the shared shell', async () => {
    mount(); await screen.findByRole('searchbox',{name:'Filter runs'})
    expect(screen.getByRole('link',{name:'Dashboard'})).toHaveAttribute('aria-current','page')
    expect(screen.getByRole('link',{name:'Recovery'})).toHaveAttribute('href','/ops/recovery')
    if (process.env.DASHBOARD_VISUAL_SNAPSHOT) writeFileSync('.dashboard-review.html', `<!doctype html><html data-theme="dark"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Dashboard layout review — test fixture</title><link rel="stylesheet" href="/src/index.css"><link rel="stylesheet" href="/src/dashboard.css"></head><body>${document.body.innerHTML}<div style="position:fixed;bottom:0;right:0;padding:4px 8px;background:#12110f;color:#a98a7f;font:11px system-ui;z-index:100">Layout review · test fixture</div></body></html>`)
  })
})
