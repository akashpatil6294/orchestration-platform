import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { ApiError, api } from '../lib/api'
import type { ScheduleView } from '../lib/types'

/** Month-grid calendar of upcoming scheduled runs. */
export default function SchedulesCalendarPage() {
  const [schedules, setSchedules] = useState<ScheduleView[]>([])
  const [monthOffset, setMonthOffset] = useState(0)
  const [error, setError] = useState<string | null>(null)
  // Snapshot "today" once; the linter treats Date as impure, so keep it out of render.
  const [today] = useState(() => new Date().toDateString())

  useEffect(() => {
    api
      .get<{ items: ScheduleView[] }>('/api/v1/schedules')
      .then((data) => setSchedules(data.items.filter((s) => s.enabled)))
      .catch((cause) => setError(cause instanceof ApiError ? cause.message : 'Could not load schedules.'))
  }, [])

  const { weeks, monthLabel, dayRuns } = useMemo(() => buildCalendar(schedules, monthOffset), [schedules, monthOffset])

function buildCalendar(schedules: ScheduleView[], monthOffset: number) {
  const now = new Date()
  now.setMonth(now.getMonth() + monthOffset)
  const first = new Date(now.getFullYear(), now.getMonth(), 1)
  const monthLabel = first.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
  const startDay = (first.getDay() + 6) % 7 // Monday-first
  const daysInMonth = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate()
  const cells: (Date | null)[] = []
  for (let i = 0; i < startDay; i++) cells.push(null)
  for (let d = 1; d <= daysInMonth; d++) cells.push(new Date(first.getFullYear(), first.getMonth(), d))
  while (cells.length % 7 !== 0) cells.push(null)
  const weeks: (Date | null)[][] = []
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7))
  
  const dayRuns = new Map<string, ScheduleView[]>()
  const key = (d: Date) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`
  schedules.forEach((schedule) => {
    if (!schedule.next_run_at) return
    const next = new Date(schedule.next_run_at)
    // Show the next occurrence; a full cron expansion lives on the schedules page preview.
    if (next.getFullYear() === first.getFullYear() && next.getMonth() === first.getMonth()) {
      const k = key(next)
      if (!dayRuns.has(k)) dayRuns.set(k, [])
      dayRuns.get(k)?.push(schedule)
    }
  })
  return { weeks, monthLabel, dayRuns }
}

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Schedule calendar</h1>
          <p className="muted">Upcoming runs for enabled schedules. Use the schedules page for cron editing.</p>
        </div>
        <div className="page-actions">
          <button type="button" className="button button-ghost button-sm" onClick={() => setMonthOffset((o) => o - 1)}>
            ← Prev
          </button>
          <button type="button" className="button button-ghost button-sm" onClick={() => setMonthOffset(0)}>
            Today
          </button>
          <button type="button" className="button button-ghost button-sm" onClick={() => setMonthOffset((o) => o + 1)}>
            Next →
          </button>
        </div>
      </div>
      {error ? (
        <div className="banner banner-error" role="alert">
          {error}
        </div>
      ) : null}
      <section className="panel">
        <h2>{monthLabel}</h2>
        <div className="calendar-grid" role="grid" aria-label={monthLabel}>
          {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((d) => (
            <div key={d} className="calendar-dow">
              {d}
            </div>
          ))}
          {weeks.flat().map((date, i) => {
            const runs = date ? dayRuns.get(`${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`) ?? [] : []
            const isToday = date ? date.toDateString() === today : false
            return (
              <div key={i} className={`calendar-cell${isToday ? ' calendar-today' : ''}`} role="gridcell">
                <div className="calendar-date">{date?.getDate() ?? ''}</div>
                {runs.slice(0, 3).map((s) => (
                  <Link key={s.id} to="/schedules" className="calendar-run" title={`${s.workflow_name}: ${s.cron_expression}`}>
                    {s.workflow_name}
                  </Link>
                ))}
                {runs.length > 3 ? <div className="muted small">+{runs.length - 3} more</div> : null}
              </div>
            )
          })}
        </div>
      </section>
    </div>
  )
}
