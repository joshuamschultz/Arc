import { cn } from '@/lib/utils'

/**
 * Minimal inline sparkline for a stat tile — no axes or grid, just the shape of
 * a per-bucket series with a faint area fill echoing the full AreaSeries chart.
 * Token-driven; stretches to fill its slot (h-8 on the InsightStat spark row).
 */
export function Sparkline({
  data,
  color = 'var(--primary)',
  className,
}: {
  data: number[]
  color?: string
  className?: string
}) {
  const pts = data.filter((n) => Number.isFinite(n))
  if (pts.length < 2) return null

  const w = 100
  const h = 28
  const max = Math.max(...pts)
  const min = Math.min(...pts)
  const span = max - min || 1
  const step = w / (pts.length - 1)
  const coords = pts.map((v, i) => {
    const x = i * step
    const y = h - ((v - min) / span) * (h - 4) - 2
    return [x, y] as const
  })
  const points = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`)
  const line = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p}`).join(' ')
  const area = `M${coords[0][0].toFixed(1)},${h} ${points
    .map((p) => `L${p}`)
    .join(' ')} L${w},${h} Z`

  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="none"
      className={cn('h-full w-full', className)}
      aria-hidden
    >
      <path d={area} fill={color} fillOpacity={0.12} stroke="none" />
      <path
        d={line}
        fill="none"
        stroke={color}
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  )
}
