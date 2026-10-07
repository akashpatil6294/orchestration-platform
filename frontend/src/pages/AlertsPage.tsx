/**
 * Alerts page: fired alerts with filters, rule management, and cost budgets.
 * (Stage H, H4)
 */
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../lib/api'
import { Badge, Button, Card, EmptyState } from '../components/ui'

interface AlertRule {
  id: string
  name: string
  workflow_id: string | null
  condition: string
  params: Record<string, unknown>
  channel_id: string | null
  cooldown_seconds: number
  is_active: boolean
  last_fired_at: string | null
}

interface AlertEvent {
  id: string
  rule_id: string | null
  rule_name: string | null
  workflow_id: string | null
  run_id: string | null
  condition: string
  message: string
  details: Record<string, unknown>
  acknowledged_at: string | null
  acknowledged_by: string | null
  created_at: string
}

const CONDITION_LABELS: Record<string, string> = {
  step_failed: 'Step failed',
  run_failed: 'Run failed',
  no_successful_run: 'No successful run',
  cost_exceeds: 'Cost exceeds',
  budget_warning: 'Budget warning',
  budget_hard_stop: 'Budget hard stop',
}

export default function AlertsPage() {
  const [alerts, setAlerts] = useState<AlertEvent[]>([])
  const [rules, setRules] = useState<AlertRule[]>([])
  const [conditions, setConditions] = useState<string[]>([])
  const [filter, setFilter] = useState({ condition: '', acknowledged: '', workflow_id: '' })
  const [showRuleForm, setShowRuleForm] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    try {
      const params = new URLSearchParams()
      if (filter.condition) params.set('condition', filter.condition)
      if (filter.acknowledged) params.set('acknowledged', filter.acknowledged)
      if (filter.workflow_id) params.set('workflow_id', filter.workflow_id)
      const [alertData, ruleData] = await Promise.all([
        api.get<{ items: AlertEvent[] }>(`/api/v1/alerts?${params}`),
        api.get<{ items: AlertRule[]; conditions: string[] }>('/api/v1/alerts/rules'),
      ])
      setAlerts(alertData.items)
      setRules(ruleData.items)
      setConditions(ruleData.conditions)
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to load alerts')
    }
  }

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter])

  const ack = async (id: string) => {
    await api.post(`/api/v1/alerts/${id}/ack`)
    void load()
  }

  const testRule = async (id: string) => {
    const result = await api.post<{ would_fire: boolean; in_cooldown: boolean; detail: Record<string, unknown> }>(
      `/api/v1/alerts/rules/${id}/test`,
    )
    alert(
      result.would_fire
        ? `This rule WOULD fire right now. ${JSON.stringify(result.detail)}`
        : `This rule would not fire right now. ${JSON.stringify(result.detail)}`,
    )
  }

  const toggleRule = async (rule: AlertRule) => {
    await api.patch(`/api/v1/alerts/rules/${rule.id}`, { is_active: !rule.is_active })
    void load()
  }

  const deleteRule = async (id: string) => {
    if (!confirm('Delete this alert rule?')) return
    await api.remove(`/api/v1/alerts/rules/${id}`)
    void load()
  }

  return (
    <div className="page">
      <header className="page-header">
        <h1>Alerts</h1>
        <Button variant="primary" onClick={() => setShowRuleForm(true)}>New rule</Button>
      </header>

      {error && <p className="alert alert-error" role="alert">{error}</p>}

      <Card title="Fired alerts">
        <div className="filters">
          <select value={filter.condition} onChange={(e) => setFilter({ ...filter, condition: e.target.value })} aria-label="Filter by condition">
            <option value="">All conditions</option>
            {Object.entries(CONDITION_LABELS).map(([v, l]) => (
              <option key={v} value={v}>{l}</option>
            ))}
          </select>
          <select value={filter.acknowledged} onChange={(e) => setFilter({ ...filter, acknowledged: e.target.value })} aria-label="Filter by acknowledgement">
            <option value="">All</option>
            <option value="false">Unacknowledged</option>
            <option value="true">Acknowledged</option>
          </select>
          <input
            placeholder="Workflow ID"
            value={filter.workflow_id}
            onChange={(e) => setFilter({ ...filter, workflow_id: e.target.value })}
            aria-label="Filter by workflow"
          />
        </div>

        {alerts.length === 0 ? (
          <EmptyState title="No alerts" description="Rules fire when their conditions are met." />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Time</th><th>Rule</th><th>Condition</th><th>Message</th><th>Run</th><th>Status</th><th></th>
              </tr>
            </thead>
            <tbody>
              {alerts.map((a) => (
                <tr key={a.id} className={a.acknowledged_at ? 'ack' : 'unack'}>
                  <td>{new Date(a.created_at).toLocaleString()}</td>
                  <td>{a.rule_name ?? '—'}</td>
                  <td><Badge tone={a.acknowledged_at ? 'muted' : 'warning'}>{CONDITION_LABELS[a.condition] ?? a.condition}</Badge></td>
                  <td>{a.message}</td>
                  <td>{a.run_id ? <Link to={`/runs/${a.run_id}`}>{a.run_id.slice(0, 8)}</Link> : '—'}</td>
                  <td>{a.acknowledged_at ? `Acked by ${a.acknowledged_by}` : 'Open'}</td>
                  <td>
                    {!a.acknowledged_at && (
                      <button onClick={() => void ack(a.id)} className="button button-small">Acknowledge</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card title="Alert rules">
        {rules.length === 0 ? (
          <EmptyState title="No rules yet" description="Create one to get notified." />
        ) : (
          <table className="table">
            <thead>
              <tr><th>Name</th><th>Condition</th><th>Workflow</th><th>Cooldown</th><th>Last fired</th><th>Active</th><th></th></tr>
            </thead>
            <tbody>
              {rules.map((r) => (
                <tr key={r.id}>
                  <td>{r.name}</td>
                  <td>{CONDITION_LABELS[r.condition] ?? r.condition}</td>
                  <td>{r.workflow_id ? r.workflow_id.slice(0, 8) : 'All'}</td>
                  <td>{Math.round(r.cooldown_seconds / 60)}m</td>
                  <td>{r.last_fired_at ? new Date(r.last_fired_at).toLocaleString() : 'Never'}</td>
                  <td>{r.is_active ? 'Yes' : 'No'}</td>
                  <td className="row-actions">
                    <button onClick={() => void testRule(r.id)} className="button button-small" title="Dry-run: would this fire now?">Test</button>
                    <button onClick={() => void toggleRule(r)} className="button button-small">{r.is_active ? 'Disable' : 'Enable'}</button>
                    <button onClick={() => void deleteRule(r.id)} className="button button-small button-danger">Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {showRuleForm && (
        <RuleForm
          conditions={conditions}
          onClose={() => setShowRuleForm(false)}
          onCreated={() => {
            setShowRuleForm(false)
            void load()
          }}
        />
      )}
    </div>
  )
}

function RuleForm(props: { conditions: string[]; onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState('')
  const [condition, setCondition] = useState('run_failed')
  const [workflowId, setWorkflowId] = useState('')
  const [cooldown, setCooldown] = useState(60)
  const [params, setParams] = useState('{}')
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    try {
      const parsedParams = JSON.parse(params) as Record<string, unknown>
      await api.post('/api/v1/alerts/rules', {
        name: name.trim(),
        condition,
        workflow_id: workflowId.trim() || null,
        params: parsedParams,
        cooldown_seconds: cooldown * 60,
      })
      props.onCreated()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create rule')
    }
  }

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="New alert rule">
      <div className="modal">
        <h2>New alert rule</h2>
        <label>Name<input value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label>Condition
          <select value={condition} onChange={(e) => setCondition(e.target.value)}>
            {props.conditions.map((c) => (
              <option key={c} value={c}>{CONDITION_LABELS[c] ?? c}</option>
            ))}
          </select>
        </label>
        <label>Workflow ID (blank = all)<input value={workflowId} onChange={(e) => setWorkflowId(e.target.value)} /></label>
        <label>Cooldown (minutes)<input type="number" min={1} value={cooldown} onChange={(e) => setCooldown(Number(e.target.value))} /></label>
        <label>Parameters (JSON)<textarea value={params} onChange={(e) => setParams(e.target.value)} rows={3} spellCheck={false} /></label>
        {error && <p className="code-view-error" role="alert">{error}</p>}
        <div className="modal-actions">
          <button onClick={props.onClose} className="btn-ghost">Cancel</button>
          <button onClick={() => void submit()} disabled={!name.trim()}>Create rule</button>
        </div>
      </div>
    </div>
  )
}
