import { useMemo, useState } from 'react'
import { Activity, Coins, Cpu, Gauge, TrendingDown } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { StatCard } from '@/components/stat-card'
import { FilterPills } from '@/components/filter-pills'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { ChartCard, AreaSeries, BarSeries } from '@/components/charts'
import { TraceTable } from '@/components/trace-table'
import { EmptyState } from '@/components/states'
import {
  useBudgets,
  useCircuitBreakers,
  useCostEfficiency,
  useLlmStats,
  usePerformance,
  useTimeseries,
  useTraces,
} from '@/lib/queries'
import { fmtCost, fmtLatency, fmtNumber, fmtTokens } from '@/lib/format'
import type { Dict } from '@/lib/types'

const WINDOWS = [
  { value: '1h', label: '1h' },
  { value: '24h', label: '24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
]

/** Best-effort relative labels for evenly-spaced timeseries buckets. */
function bucketLabels(window: string, n: number): string[] {
  if (n <= 1) return ['now']
  const unit = window.endsWith('h') ? 'h' : window.endsWith('d') ? 'd' : ''
  const span = parseInt(window, 10) || n
  return Array.from({ length: n }, (_, i) => {
    if (i === n - 1) return 'now'
    const ago = Math.round(((n - 1 - i) / (n - 1)) * span)
    return unit ? `-${ago}${unit}` : `${i + 1}`
  })
}

function dictToSeries(dict: Dict | undefined): Array<{ label: string; value: number }> {
  if (!dict) return []
  return Object.entries(dict)
    .map(([label, v]) => ({ label, value: typeof v === 'number' ? v : 0 }))
    .filter((d) => d.value > 0)
    .sort((a, b) => b.value - a.value)
}

function Overview() {
  const [window, setWindow] = useState('7d')
  const stats = useLlmStats(window)
  const ts = useTimeseries(window === '1h' ? '1h' : window === '30d' ? '30d' : '24h')
  const breakers = useCircuitBreakers()
  const budgets = useBudgets()
  const perf = usePerformance(window)
  const eff = useCostEfficiency(window === '1h' ? '24h' : window)

  const s = stats.data
  const buckets = ts.data?.buckets ?? []
  const labels = bucketLabels(ts.data?.window ?? '24h', buckets.length)
  const volume = buckets.map((b, i) => ({
    label: labels[i],
    tokens: b.total_tokens,
    requests: b.request_count,
  }))
  const providerCost = useMemo(
    () => dictToSeries(s?.provider_costs).map((d) => ({ label: d.label, cost: d.value })),
    [s],
  )
  const modelRows = useMemo<Array<Dict & { model: string }>>(
    () => Object.entries((s?.model_stats ?? {}) as Dict).map(([model, v]) => ({ model, ...(v as Dict) })),
    [s],
  )
  const agentCost = useMemo(
    () =>
      Object.entries((s?.agent_perf ?? {}) as Dict)
        .map(([label, v]) => ({ label, cost: Number((v as Dict)?.total_cost ?? 0) }))
        .filter((d) => d.cost > 0)
        .sort((a, b) => b.cost - a.cost),
    [s],
  )
  const savings = eff.data?.potential_savings_usd ?? 0

  return (
    <div className="space-y-5">
      <FilterPills value={window} onChange={setWindow} options={WINDOWS} />

      {savings > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 rounded-lg border border-status-warning/30 bg-status-warning/10 px-4 py-2 text-sm text-foreground">
          <TrendingDown className="size-4 text-status-warning" />
          <span>Potential savings</span>
          <strong className="font-semibold tabular-nums">{fmtCost(savings)}</strong>
          {eff.data?.potential_savings_pct ? (
            <span className="text-muted-foreground">({Math.round(eff.data.potential_savings_pct)}%)</span>
          ) : null}
          <span>by switching to</span>
          <span className="inline-flex items-center rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs">
            {eff.data?.cheapest_model ?? 'the cheapest model'}
          </span>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard label="Requests" value={fmtNumber(s?.request_count ?? 0)} icon={<Activity className="size-4" />} hint={`${s?.error_count ?? 0} errors`} />
        <StatCard label="Tokens" value={fmtTokens(s?.total_tokens ?? 0)} icon={<Cpu className="size-4" />} />
        <StatCard label="Avg latency" value={fmtLatency(s?.latency_avg ?? 0)} icon={<Gauge className="size-4" />} hint={`p95 ${fmtLatency(s?.latency_p95 ?? 0)}`} />
        <StatCard label="Cost" value={fmtCost(s?.total_cost ?? 0)} icon={<Coins className="size-4" />} />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <ChartCard title="Token volume">
          {volume.some((v) => v.tokens > 0) ? (
            <AreaSeries data={volume} dataKey="tokens" />
          ) : (
            <EmptyState title="No token activity in this window" />
          )}
        </ChartCard>
        <ChartCard title="Cost by provider">
          {providerCost.length > 0 ? (
            <BarSeries data={providerCost} dataKey="cost" color="var(--chart-3)" />
          ) : (
            <EmptyState title="No cost recorded yet" />
          )}
        </ChartCard>
      </div>

      {agentCost.length > 0 && (
        <ChartCard title="Cost by agent">
          <BarSeries data={agentCost} dataKey="cost" color="var(--chart-4)" />
        </ChartCard>
      )}

      <section className="space-y-2">
        <h3 className="text-sm font-semibold text-foreground">Model performance</h3>
        {modelRows.length === 0 ? (
          <EmptyState title="No model activity yet" />
        ) : (
          <div className="overflow-hidden rounded-lg border border-border bg-card">
            <table className="w-full text-sm">
              <thead className="bg-muted/40 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="px-4 py-2.5 text-left">Model</th>
                  <th className="px-4 py-2.5 text-right">Calls</th>
                  <th className="px-4 py-2.5 text-right">Tokens</th>
                  <th className="px-4 py-2.5 text-right">Cost</th>
                </tr>
              </thead>
              <tbody>
                {modelRows.map((m) => (
                  <tr key={m.model} className="border-b border-border/60 transition-colors last:border-0 hover:bg-muted/40">
                    <td className="px-4 py-2">
                      <span className="inline-flex items-center rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-foreground">
                        {m.model}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-right tabular-nums">{fmtNumber((m.count ?? m.request_count) as number)}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{fmtTokens((m.tokens ?? m.total_tokens) as number)}</td>
                    <td className="px-4 py-2 text-right tabular-nums text-foreground">{fmtCost((m.cost ?? m.total_cost) as number)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="rounded-lg border border-border bg-card p-4 shadow-xs">
          <h3 className="mb-3 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Circuit breakers</h3>
          {(breakers.data?.circuit_breakers ?? []).length === 0 ? (
            <p className="text-xs text-muted-foreground">All circuits closed.</p>
          ) : (
            <ul className="divide-y divide-border/60">
              {(breakers.data?.circuit_breakers ?? []).map((cb, i) => (
                <li key={i} className="flex items-center justify-between py-1.5 text-sm first:pt-0 last:pb-0">
                  <span className="inline-flex items-center rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-foreground">{String((cb as Dict).name ?? '—')}</span>
                  <span className="capitalize text-muted-foreground">{String((cb as Dict).state ?? '—')}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="rounded-lg border border-border bg-card p-4 shadow-xs">
          <h3 className="mb-3 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Budgets</h3>
          {(budgets.data?.budgets ?? []).length === 0 ? (
            <p className="text-xs text-muted-foreground">No budgets configured.</p>
          ) : (
            <ul className="divide-y divide-border/60">
              {(budgets.data?.budgets ?? []).map((b, i) => (
                <li key={i} className="flex items-center justify-between py-1.5 text-sm first:pt-0 last:pb-0">
                  <span className="inline-flex items-center rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-foreground">{String((b as Dict).name ?? `budget ${i + 1}`)}</span>
                  <span className="tabular-nums text-muted-foreground">{fmtCost(((b as Dict).used ?? (b as Dict).spent) as number)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="rounded-lg border border-border bg-card p-4 shadow-xs">
          <h3 className="mb-3 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Cost efficiency</h3>
          <div className="divide-y divide-border/60 text-sm">
            <div className="flex items-center justify-between py-1.5 first:pt-0">
              <span className="text-muted-foreground">Potential savings</span>
              <span className="tabular-nums text-foreground">{fmtCost(eff.data?.potential_savings_usd ?? 0)}</span>
            </div>
            <div className="flex items-center justify-between py-1.5">
              <span className="text-muted-foreground">Cheapest model</span>
              <span className="inline-flex items-center rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-foreground">{eff.data?.cheapest_model ?? '—'}</span>
            </div>
            <div className="flex items-center justify-between py-1.5 last:pb-0">
              <span className="text-muted-foreground">Most used</span>
              <span className="inline-flex items-center rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-foreground">{eff.data?.most_used_model ?? '—'}</span>
            </div>
          </div>
        </div>
      </div>

      {perf.data?.agents && perf.data.agents.length > 0 && (
        <p className="text-xs text-muted-foreground">{perf.data.agents.length} agents active in window.</p>
      )}
    </div>
  )
}

function Calls() {
  const traceQuery = useTraces(200)
  const traces = traceQuery.data?.traces ?? []
  return <TraceTable traces={traces} />
}

export function ArcLlmPage() {
  return (
    <div className="flex h-full flex-col">
      <PageHeader title="ArcLLM" description="LLM-call telemetry — requests, responses, cost, and latency." />
      <Tabs defaultValue="overview" className="flex flex-1 flex-col overflow-hidden">
        <div className="border-b border-border px-6">
          <TabsList className="my-2">
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="calls">Calls</TabsTrigger>
          </TabsList>
        </div>
        <TabsContent value="overview" className="flex-1 overflow-auto p-6">
          <Overview />
        </TabsContent>
        <TabsContent value="calls" className="flex-1 overflow-auto p-6">
          <Calls />
        </TabsContent>
      </Tabs>
    </div>
  )
}
