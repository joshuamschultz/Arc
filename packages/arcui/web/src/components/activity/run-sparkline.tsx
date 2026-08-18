/**
 * A tiny area sparkline of recent run durations — fills the InsightStat spark
 * slot on the Activity screen with real telemetry (no fabricated series). Reads
 * as a quiet trend line: faint solid fill, one confident stroke, emphasized
 * endpoint (REDESIGN §12.4). Token-driven (blue = the one accent). Renders
 * nothing when there is too little to plot.
 */
export function RunSparkline({ values }: { values: number[] }) {
  if (values.length < 2) return null

  const w = 100
  const h = 32
  const max = Math.max(...values)
  const min = Math.min(...values)
  const span = max - min || 1
  const step = w / (values.length - 1)
  const y = (v: number) => h - 3 - ((v - min) / span) * (h - 6)

  const pts = values.map((v, i) => [i * step, y(v)] as const)
  const line = pts.map(([px, py], i) => `${i === 0 ? 'M' : 'L'}${px.toFixed(1)} ${py.toFixed(1)}`).join(' ')
  const area = `${line} L${w} ${h} L0 ${h} Z`
  const [ex, ey] = pts[pts.length - 1]

  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="none"
      className="h-full w-full text-primary"
      aria-hidden
    >
      <path d={area} className="fill-primary/10" />
      <path d={line} fill="none" stroke="currentColor" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      <circle cx={ex} cy={ey} r="2" fill="currentColor" />
    </svg>
  )
}
