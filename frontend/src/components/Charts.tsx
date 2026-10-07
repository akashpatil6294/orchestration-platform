/**
 * Two small inline SVG charts for the dashboard: runs over time (stacked bars
 * per bucket) and the average duration trend (line).
 *
 * Hand-rolled SVG instead of a charting library: the dashboard needs exactly
 * two static series, and avoiding the dependency keeps the bundle small.
 * Both charts render a visually hidden data table so screen-reader and
 * no-SVG users get the same numbers.
 */
import type { DurationBucket, TimelineBucket } from '../lib/types'

function summarize(buckets: TimelineBucket[]): string {
  const started = buckets.reduce((sum, bucket) => sum + bucket.started, 0)
  const succeeded = buckets.reduce((sum, bucket) => sum + bucket.succeeded, 0)
  const failed = buckets.reduce((sum, bucket) => sum + bucket.failed, 0)
  return `Runs over time: ${started} started, ${succeeded} succeeded, ${failed} failed across ${buckets.length} buckets.`
}

export function RunsTimelineChart({ buckets }: { buckets: TimelineBucket[] }) {
  const width = 480
  const height = 120
  const padding = { top: 8, right: 4, bottom: 18, left: 4 }
  const innerWidth = width - padding.left - padding.right
  const innerHeight = height - padding.top - padding.bottom

  if (buckets.length === 0) return <p className="muted">No runs in this window.</p>

  const totals = buckets.map((bucket) => bucket.started)
  const max = Math.max(1, ...totals)
  const slot = innerWidth / buckets.length
  const barWidth = Math.max(2, Math.min(18, slot * 0.6))

  return (
    <figure className="chart" data-testid="runs-timeline-chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={summarize(buckets)} preserveAspectRatio="none">
        {buckets.map((bucket, index) => {
          const x = padding.left + index * slot + (slot - barWidth) / 2
          const scale = (value: number) => (value / max) * innerHeight
          const failedHeight = scale(bucket.failed)
          const succeededHeight = scale(bucket.succeeded)
          const otherHeight = Math.max(0, scale(bucket.started) - failedHeight - succeededHeight)
          const base = padding.top + innerHeight
          const total = bucket.started
          return (
            <g key={bucket.bucket_start}>
              <rect
                x={x}
                y={base - failedHeight}
                width={barWidth}
                height={failedHeight}
                className="chart-bar chart-bar-failed"
              >
                <title>{`${bucket.label}: ${bucket.failed} failed`}</title>
              </rect>
              <rect
                x={x}
                y={base - failedHeight - otherHeight}
                width={barWidth}
                height={otherHeight}
                className="chart-bar chart-bar-other"
              >
                <title>{`${bucket.label}: ${total - bucket.succeeded - bucket.failed} other`}</title>
              </rect>
              <rect
                x={x}
                y={base - failedHeight - otherHeight - succeededHeight}
                width={barWidth}
                height={succeededHeight}
                className="chart-bar chart-bar-succeeded"
              >
                <title>{`${bucket.label}: ${bucket.succeeded} succeeded`}</title>
              </rect>
              {(index === 0 || index === buckets.length - 1 || index === Math.floor(buckets.length / 2)) && (
                <text x={x + barWidth / 2} y={height - 4} textAnchor="middle" className="chart-label">
                  {bucket.label}
                </text>
              )}
            </g>
          )
        })}
      </svg>
      <figcaption className="chart-legend">
        <span className="legend-item">
          <span className="legend-swatch legend-succeeded" /> Succeeded
        </span>
        <span className="legend-item">
          <span className="legend-swatch legend-other" /> Other
        </span>
        <span className="legend-item">
          <span className="legend-swatch legend-failed" /> Failed
        </span>
      </figcaption>
      <table className="visually-hidden">
        <caption>Runs per time bucket</caption>
        <thead>
          <tr>
            <th scope="col">Bucket</th>
            <th scope="col">Started</th>
            <th scope="col">Succeeded</th>
            <th scope="col">Failed</th>
          </tr>
        </thead>
        <tbody>
          {buckets.map((bucket) => (
            <tr key={bucket.bucket_start}>
              <th scope="row">{bucket.label}</th>
              <td>{bucket.started}</td>
              <td>{bucket.succeeded}</td>
              <td>{bucket.failed}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}

export function DurationTrendChart({ buckets }: { buckets: DurationBucket[] }) {
  const width = 480
  const height = 110
  const padding = { top: 10, right: 6, bottom: 18, left: 6 }
  const innerWidth = width - padding.left - padding.right
  const innerHeight = height - padding.top - padding.bottom

  const values = buckets.map((bucket) => bucket.avg_duration_seconds)
  const present = values.filter((value): value is number => value !== null)
  if (present.length === 0) return <p className="muted">No finished runs in this window yet.</p>

  const max = Math.max(...present)
  const slot = innerWidth / Math.max(1, buckets.length - 1)
  const point = (index: number, value: number) => ({
    x: padding.left + index * slot,
    y: padding.top + innerHeight - (value / max) * innerHeight,
  })

  let path = ''
  let open = false
  values.forEach((value, index) => {
    if (value === null) {
      open = false
      return
    }
    const { x, y } = point(index, value)
    path += `${open ? ' L' : ' M'}${x.toFixed(1)},${y.toFixed(1)}`
    open = true
  })

  return (
    <figure className="chart" data-testid="duration-trend-chart">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`Average run duration trend across ${buckets.length} buckets, peak ${max.toFixed(1)} seconds.`}
        preserveAspectRatio="none"
      >
        <path d={path} className="chart-line" fill="none" />
        {values.map((value, index) =>
          value === null ? null : <circle key={buckets[index].bucket_start} {...point(index, value)} r={2.5} className="chart-dot" />,
        )}
        {buckets.map(
          (bucket, index) =>
            (index === 0 || index === buckets.length - 1 || index === Math.floor(buckets.length / 2)) && (
              <text
                key={bucket.bucket_start}
                x={point(index, values[index] ?? 0).x}
                y={height - 4}
                textAnchor="middle"
                className="chart-label"
              >
                {bucket.label}
              </text>
            ),
        )}
      </svg>
      <table className="visually-hidden">
        <caption>Average run duration per bucket (seconds)</caption>
        <thead>
          <tr>
            <th scope="col">Bucket</th>
            <th scope="col">Average duration</th>
          </tr>
        </thead>
        <tbody>
          {buckets.map((bucket) => (
            <tr key={bucket.bucket_start}>
              <th scope="row">{bucket.label}</th>
              <td>{bucket.avg_duration_seconds === null ? '—' : bucket.avg_duration_seconds}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}
