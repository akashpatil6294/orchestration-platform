import { useEffect, useState } from 'react'
import { api } from '../lib/api'

interface QuotaInfo {
  limit: number
  usage: number
}

interface QuotaStatus {
  runs_per_day: QuotaInfo
  concurrent_runs: QuotaInfo
}

function Meter({ label, info }: { label: string; info: QuotaInfo }) {
  if (info.limit === 0) return null
  const pct = Math.min(100, Math.round((info.usage / info.limit) * 100))
  const over = info.usage >= info.limit
  return (
    <div className="quota-meter" title={`${info.usage} of ${info.limit} used`}>
      <div className="quota-meter-header">
        <span>{label}</span>
        <span className={over ? 'quota-over' : ''}>
          {info.usage} / {info.limit}
        </span>
      </div>
      <div className="quota-meter-bar">
        <div
          className={`quota-meter-fill${over ? ' quota-meter-fill-over' : ''}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

export default function QuotaMeters() {
  const [quota, setQuota] = useState<QuotaStatus | null>(null)

  useEffect(() => {
    api
      .get<QuotaStatus>('/api/v1/auth/quota')
      .then(setQuota)
      .catch(() => setQuota(null))
  }, [])

  if (!quota || !quota.runs_per_day || !quota.concurrent_runs) return null
  return (
    <div className="quota-meters" aria-label="Quota usage">
      <Meter label="Runs today" info={quota.runs_per_day} />
      <Meter label="Concurrent runs" info={quota.concurrent_runs} />
    </div>
  )
}
