import type { ReactNode } from 'react'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { fmtCompact } from '@/lib/format'

// Recharts renders raw axis values by default (a token count shows as an
// unreadable "1834221", and a flat/degenerate domain collapses to "0000").
// Every numeric series axis + tooltip runs through a compact formatter (1.2K,
// 3.4M) unless a caller passes its own — one place, so no chart regresses to
// raw numbers again (H-003).
const _num = (v: unknown): number => (typeof v === 'number' ? v : Number(v))

const AXIS = { fontSize: 11, fill: 'var(--muted-foreground)' }
const TOOLTIP_STYLE = {
  background: 'var(--popover)',
  border: '1px solid var(--border)',
  borderRadius: 'var(--radius)',
  boxShadow: 'var(--shadow-md)',
  fontSize: '12px',
  padding: '6px 10px',
  color: 'var(--popover-foreground)',
}
const TOOLTIP_LABEL_STYLE = {
  color: 'var(--muted-foreground)',
  fontSize: '10px',
  textTransform: 'uppercase' as const,
  letterSpacing: '0.08em',
  marginBottom: '2px',
}
const TOOLTIP_ITEM_STYLE = { color: 'var(--popover-foreground)', padding: 0 }

/** Titled card wrapper for a chart. */
export function ChartCard({
  title,
  action,
  children,
}: {
  title: string
  action?: ReactNode
  children: ReactNode
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-4 shadow-xs transition-colors duration-150 hover:border-primary/25">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          {title}
        </h3>
        {action}
      </div>
      <div className="h-56">{children}</div>
    </div>
  )
}

/** Area chart over a numeric series keyed by `dataKey`. */
export function AreaSeries({
  data,
  dataKey,
  color = 'var(--chart-1)',
  valueFormat = fmtCompact,
}: {
  data: Array<Record<string, unknown>>
  dataKey: string
  color?: string
  valueFormat?: (n: number) => string
}) {
  const gradId = `grad-${dataKey}`
  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={data} margin={{ top: 4, right: 4, left: -16, bottom: 0 }}>
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity={0.28} />
            <stop offset="100%" stopColor={color} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke="var(--border)" strokeOpacity={0.5} vertical={false} />
        <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={false} minTickGap={24} />
        <YAxis
          tick={AXIS}
          tickLine={false}
          axisLine={false}
          width={48}
          tickFormatter={(v: number) => valueFormat(_num(v))}
        />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelStyle={TOOLTIP_LABEL_STYLE}
          itemStyle={TOOLTIP_ITEM_STYLE}
          cursor={{ stroke: 'var(--border)', strokeWidth: 1 }}
          formatter={(v: unknown) => valueFormat(_num(v))}
        />
        <Area
          type="monotone"
          dataKey={dataKey}
          stroke={color}
          strokeWidth={1.5}
          fill={`url(#${gradId})`}
          dot={false}
          activeDot={{ r: 3, strokeWidth: 0, fill: color }}
        />
      </AreaChart>
    </ResponsiveContainer>
  )
}

/** Horizontal-ish bar chart for categorical totals (e.g. cost by provider). */
export function BarSeries({
  data,
  dataKey,
  color = 'var(--chart-2)',
  valueFormat = fmtCompact,
}: {
  data: Array<Record<string, unknown>>
  dataKey: string
  color?: string
  valueFormat?: (n: number) => string
}) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ top: 4, right: 4, left: -16, bottom: 0 }}>
        <CartesianGrid stroke="var(--border)" strokeOpacity={0.5} vertical={false} />
        <XAxis dataKey="label" tick={AXIS} tickLine={false} axisLine={false} />
        <YAxis
          tick={AXIS}
          tickLine={false}
          axisLine={false}
          width={48}
          tickFormatter={(v: number) => valueFormat(_num(v))}
        />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelStyle={TOOLTIP_LABEL_STYLE}
          itemStyle={TOOLTIP_ITEM_STYLE}
          cursor={{ fill: 'var(--muted)', opacity: 0.3 }}
          formatter={(v: unknown) => valueFormat(_num(v))}
        />
        <Bar dataKey={dataKey} fill={color} radius={[3, 3, 0, 0]} maxBarSize={48} />
      </BarChart>
    </ResponsiveContainer>
  )
}
