import { useState, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { StatusDot } from '@/components/status-badge'
import { StatCard } from '@/components/stat-card'
import { DataTable } from '@/components/data-table'
import { TraceTable } from '@/components/trace-table'
import { FileTree } from '@/components/file-tree'
import { Markdown } from '@/components/markdown'
import { PolicyBulletCard } from '@/components/policy-bullet'
import {
  ScoreDistribution,
  TopPerformers,
  PolicyConfigCards,
  SystemPolicyRules,
} from '@/components/policy-views'
import { RunReplayDrawer } from '@/components/run-replay-drawer'
import { CapabilityTable } from '@/components/capability-table'
import { ToolsTable } from '@/components/tools-table'
import { AreaSeries } from '@/components/charts'
import { QueryState, EmptyState } from '@/components/states'
import {
  useAgent,
  useAgentCapabilities,
  useAgentConfig,
  useAgentPolicy,
  useAgentPolicyStats,
  useAgentSchedules,
  useAgentSessions,
  useAgentStats,
  useAgentTasks,
  useAgentTimeseries,
  useAgentTools,
  useAgentTraces,
} from '@/lib/queries'
import {
  fmtBytes,
  fmtCost,
  fmtLatency,
  fmtNumber,
  initials,
  relativeTime,
  shortId,
} from '@/lib/format'
import { cn } from '@/lib/utils'
import type { ColumnDef } from '@tanstack/react-table'
import type { Dict } from '@/lib/types'

const TABS = [
  'overview', 'identity', 'sessions', 'llm', 'skills', 'tools', 'policy', 'workspace', 'files',
] as const
type TabId = (typeof TABS)[number]

// --- Small building blocks -------------------------------------------------

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      {children}
    </section>
  )
}

/** Titled card used across the detail tabs. */
function InfoCard({ title, extra, children }: { title: string; extra?: ReactNode; children: ReactNode }) {
  return (
    <div className="rounded-xl border border-border bg-card p-4 shadow-xs">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
        {extra}
      </div>
      {children}
    </div>
  )
}

/** Label/value rows — the old kv-grid, restyled for the React shell. */
function KVList({ rows }: { rows: Array<[string, ReactNode]> }) {
  return (
    <dl className="divide-y divide-border/60">
      {rows.map(([k, v], i) => (
        <div key={i} className="flex items-center justify-between gap-3 py-1.5 text-sm first:pt-0 last:pb-0">
          <dt className="shrink-0 text-xs text-muted-foreground">{k}</dt>
          <dd className="min-w-0 truncate text-right font-mono text-foreground">{v ?? '—'}</dd>
        </div>
      ))}
    </dl>
  )
}

function Pill({ tone, children }: { tone: 'online' | 'neutral'; children: ReactNode }) {
  return (
    <span
      className={cn(
        'rounded-full px-2 py-0.5 text-[11px] font-medium',
        tone === 'online'
          ? 'bg-status-online/15 text-status-online'
          : 'bg-muted text-muted-foreground',
      )}
    >
      {children}
    </span>
  )
}

function MiniBar({ pct, tone = 'online' }: { pct: number; tone?: 'online' | 'warning' | 'error' }) {
  const w = Math.max(0, Math.min(100, pct))
  const bg = tone === 'error' ? 'bg-status-error' : tone === 'warning' ? 'bg-status-warning' : 'bg-status-online'
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
      <div className={cn('h-full rounded-full transition-all', bg)} style={{ width: `${w}%` }} />
    </div>
  )
}

function MetricTile({ label, value, pct, tone }: { label: string; value: string; pct: number; tone?: 'online' | 'warning' | 'error' }) {
  return (
    <div className="space-y-1.5 text-center">
      <div className="text-lg font-semibold tabular-nums text-foreground">{value}</div>
      <MiniBar pct={pct} tone={tone} />
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</div>
    </div>
  )
}

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

// --- Tabs ------------------------------------------------------------------

function OverviewTab({ agentId }: { agentId: string }) {
  const agent = useAgent(agentId)
  const config = useAgentConfig(agentId)
  const stats = useAgentStats(agentId, '24h')
  const sessions = useAgentSessions(agentId)
  const tasks = useAgentTasks(agentId)
  const schedules = useAgentSchedules(agentId)
  const ts = useAgentTimeseries(agentId, '24h')
  const traces = useAgentTraces(agentId, 1)
  const [activeSession, setActiveSession] = useState<string | null>(null)

  const a = agent.data ?? {}
  const cfg = (config.data?.config ?? {}) as Dict
  const llm = (cfg.llm ?? {}) as Dict
  const ctx = (cfg.context ?? {}) as Dict
  const tel = (cfg.telemetry ?? {}) as Dict
  const toolsPolicy = ((cfg.tools as Dict)?.policy ?? {}) as Dict
  const s = (stats.data?.stats ?? {}) as Dict

  const online = Boolean(a.online)
  const totalCtx = Number(ctx.max_tokens ?? llm.context_window ?? 0)
  const pruneThr = Number(ctx.prune_threshold ?? 0.7)
  const compactThr = Number(ctx.compact_threshold ?? 0.85)
  const emergencyThr = Number(ctx.emergency_threshold ?? 0.95)
  const latest = (traces.data?.traces ?? [])[0] as Dict | undefined
  const used = Number(latest?.prompt_tokens ?? latest?.input_tokens ?? 0)
  const available = Math.max(0, totalCtx - used)
  const ctxPct = totalCtx > 0 ? Math.min(100, Math.round((used / totalCtx) * 100)) : 0
  const ctxTone = ctxPct >= emergencyThr * 100 ? 'error' : ctxPct >= compactThr * 100 ? 'warning' : 'online'

  const calls = Number(s.request_count ?? 0)
  const errorCount = Number(s.error_count ?? 0)
  const latencyAvg = Math.round(Number(s.latency_avg ?? 0))
  const latencyP95 = Math.round(Number(s.latency_p95 ?? 0))
  const totalCost = Number(s.total_cost ?? 0)
  const successPct = calls > 0 ? Math.round(((calls - errorCount) / calls) * 100) : 0
  const uptimePct = online ? 100 : calls > 0 ? successPct : 0
  const responsePct = latencyAvg > 0 ? Math.max(5, Math.min(100, Math.round(100 - (latencyAvg / 5000) * 100))) : 0
  const responseLabel = latencyAvg > 0 ? fmtLatency(latencyAvg) : '—'

  const buckets = ts.data?.buckets ?? []
  const labels = bucketLabels(ts.data?.window ?? '24h', buckets.length)
  const volume = buckets.map((b, i) => ({ label: labels[i], tokens: b.total_tokens }))

  const sessionRows = (sessions.data?.sessions ?? []).slice(0, 6)
  const taskList = tasks.data?.tasks ?? []
  const scheduleList = schedules.data?.schedules ?? []

  return (
    <QueryState query={agent} isEmpty={() => !agent.data}>
      {() => (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {/* Left column */}
          <div className="space-y-4">
            <InfoCard
              title="Cryptographic Identity"
              extra={<Pill tone={online ? 'online' : 'neutral'}>{online ? 'live' : 'offline'}</Pill>}
            >
              <KVList
                rows={[
                  ['DID', <span className="text-status-online">{String(a.did ?? '—')}</span>],
                  ['Organization', String(a.org ?? '—')],
                  ['Agent Type', String(a.type ?? '—')],
                  ['Display Name', String(a.display_name ?? a.name ?? '—')],
                  ['Status', <Pill tone={online ? 'online' : 'neutral'}>{online ? 'online' : 'offline'}</Pill>],
                  ['Workspace', String(a.workspace_path ?? '—')],
                ]}
              />
            </InfoCard>

            <InfoCard title="Configuration">
              <KVList
                rows={[
                  ['Model', String(llm.model ?? '—')],
                  ['Provider', String(llm.provider ?? a.provider ?? '—')],
                  ['Max Tokens', llm.max_tokens != null ? fmtNumber(Number(llm.max_tokens)) : '—'],
                  ['Temperature', llm.temperature != null ? String(llm.temperature) : '—'],
                  ['Context Window', totalCtx ? fmtNumber(totalCtx) : '—'],
                  ['Tool Timeout', toolsPolicy.timeout_seconds != null ? `${toolsPolicy.timeout_seconds}s` : '—'],
                  ['Telemetry', tel.enabled ? 'enabled' : '—'],
                  ['Service', String(tel.service_name ?? '—')],
                ]}
              />
            </InfoCard>

            <InfoCard title="Context Window" extra={<span className="text-xs text-muted-foreground">last prompt</span>}>
              <div className="mb-1.5 flex justify-between text-xs text-muted-foreground">
                <span>Last prompt size</span>
                <span className="tabular-nums">{totalCtx ? `${ctxPct}%` : '—'}</span>
              </div>
              <MiniBar pct={ctxPct} tone={ctxTone} />
              <div className="mt-3 grid grid-cols-3 gap-3 text-sm">
                <div>
                  <div className="text-[11px] text-muted-foreground">Used</div>
                  <div className="font-semibold tabular-nums text-foreground">{totalCtx ? fmtNumber(used) : '—'}</div>
                </div>
                <div>
                  <div className="text-[11px] text-muted-foreground">Available</div>
                  <div className="font-semibold tabular-nums text-foreground">{totalCtx ? fmtNumber(available) : '—'}</div>
                </div>
                <div>
                  <div className="text-[11px] text-muted-foreground">Total</div>
                  <div className="font-semibold tabular-nums text-foreground">{totalCtx ? fmtNumber(totalCtx) : '—'}</div>
                </div>
              </div>
              <div className="mt-3 flex flex-wrap gap-3 rounded-lg bg-muted/40 p-2 text-xs">
                <span className="text-muted-foreground">Thresholds</span>
                <span><span className="text-status-online">●</span> Prune {Math.round(pruneThr * 100)}%</span>
                <span><span className="text-status-warning">●</span> Compact {Math.round(compactThr * 100)}%</span>
                <span><span className="text-status-error">●</span> Emergency {Math.round(emergencyThr * 100)}%</span>
              </div>
            </InfoCard>
          </div>

          {/* Right column */}
          <div className="space-y-4">
            <InfoCard title="Performance (24h)">
              <div className="grid grid-cols-3 gap-4">
                <MetricTile label="Uptime" value={`${uptimePct}%`} pct={uptimePct} />
                <MetricTile label="Avg Response" value={responseLabel} pct={responsePct} />
                <MetricTile label="Tool Success" value={`${successPct}%`} pct={successPct} tone={successPct >= 90 ? 'online' : successPct >= 70 ? 'warning' : 'error'} />
              </div>
              <div className="mt-4">
                <div className="mb-1 text-xs text-muted-foreground">Token usage (24h)</div>
                {volume.some((v) => v.tokens > 0) ? (
                  <div className="h-32">
                    <AreaSeries data={volume} dataKey="tokens" />
                  </div>
                ) : (
                  <div className="flex h-20 items-center justify-center rounded-lg bg-muted/40 text-xs text-muted-foreground">No 24h activity</div>
                )}
              </div>
              <div className="mt-3 text-xs text-muted-foreground">
                Total cost <span className="text-foreground">{fmtCost(totalCost)}</span> · P95{' '}
                <span className="font-mono text-foreground">{latencyP95}ms</span> · Calls{' '}
                <span className="font-mono text-foreground">{fmtNumber(calls)}</span>
              </div>
            </InfoCard>

            <InfoCard title={`Recent Sessions${sessionRows.length ? ` (${sessionRows.length})` : ''}`}>
              {sessionRows.length === 0 ? (
                <p className="text-xs text-muted-foreground">No sessions yet</p>
              ) : (
                <div className="divide-y divide-border/60">
                  {sessionRows.map((sess) => (
                    <button
                      key={sess.sid}
                      type="button"
                      onClick={() => setActiveSession(sess.sid)}
                      className="flex w-full cursor-pointer items-center justify-between gap-3 py-1.5 text-left first:pt-0 last:pb-0 hover:text-foreground"
                    >
                      <span className="truncate font-mono text-xs text-primary">{shortId(sess.sid, 22)}</span>
                      <span className="shrink-0 text-xs text-muted-foreground">{relativeTime(sess.mtime)}</span>
                      <span className="shrink-0 font-mono text-xs text-muted-foreground">{fmtBytes(sess.size)}</span>
                    </button>
                  ))}
                </div>
              )}
            </InfoCard>

            <InfoCard title={`Tasks (${taskList.length})`}>
              {taskList.length === 0 ? (
                <p className="text-xs text-muted-foreground">No tasks</p>
              ) : (
                <div className="space-y-1.5">
                  {taskList.slice(0, 8).map((t, i) => (
                    <div key={i} className="flex items-center justify-between gap-3 text-sm">
                      <span className="min-w-0 truncate text-foreground">{String(t.subject ?? t.id ?? 'task')}</span>
                      {t.status != null && (
                        <span className="shrink-0 rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">{String(t.status)}</span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </InfoCard>

            <InfoCard title={`Schedules (${scheduleList.length})`}>
              {scheduleList.length === 0 ? (
                <p className="text-xs text-muted-foreground">No schedules</p>
              ) : (
                <div className="space-y-1.5">
                  {scheduleList.slice(0, 8).map((sc, i) => {
                    const sched = sc as Dict
                    return (
                      <div key={i} className="flex items-center justify-between gap-3 text-sm">
                        <span className="min-w-0 truncate text-foreground">{String(sched.name ?? sched.id ?? 'schedule')}</span>
                        {(sched.cron ?? sched.schedule ?? sched.next_run) != null && (
                          <span className="shrink-0 font-mono text-[11px] text-muted-foreground">{String(sched.cron ?? sched.schedule ?? sched.next_run)}</span>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
            </InfoCard>
          </div>

          <RunReplayDrawer
            agentId={agentId}
            sid={activeSession}
            open={!!activeSession}
            onOpenChange={(o) => !o && setActiveSession(null)}
          />
        </div>
      )}
    </QueryState>
  )
}

function IdentityTab({ agentId }: { agentId: string }) {
  const agent = useAgent(agentId)
  const config = useAgentConfig(agentId)
  const a = agent.data ?? {}
  const cfg = (config.data?.config ?? {}) as Dict
  const ident = (cfg.identity ?? {}) as Dict
  const toolsPolicy = ((cfg.tools as Dict)?.policy ?? {}) as Dict
  const allow = (toolsPolicy.allow as string[]) ?? []
  const deny = (toolsPolicy.deny as string[]) ?? []
  const color = String(a.color ?? '')

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <div className="space-y-4">
        <InfoCard title="DID" extra={<Pill tone="online">parsed</Pill>}>
          <code className="block break-all rounded-lg bg-muted/40 p-3 font-mono text-sm text-status-online">
            {String(a.did ?? '—')}
          </code>
        </InfoCard>
        <InfoCard title="Identity Config">
          <KVList
            rows={[
              ['DID', String(ident.did ?? a.did ?? '—')],
              ['Key Directory', String(ident.key_dir ?? '—')],
              ['Algorithm', 'Ed25519 (RFC 8032)'],
              ['Curve', 'Curve25519'],
              ['Key Size', '256-bit (32 bytes)'],
            ]}
          />
        </InfoCard>
      </div>
      <div className="space-y-4">
        <InfoCard title="Tool Policy">
          <KVList
            rows={[
              ['Allow', allow.length ? allow.join(', ') : '∅ (deny-all)'],
              ['Deny', deny.length ? deny.join(', ') : '∅'],
              ['Timeout', toolsPolicy.timeout_seconds != null ? `${toolsPolicy.timeout_seconds}s` : '—'],
            ]}
          />
        </InfoCard>
        <InfoCard title="Workspace">
          <KVList
            rows={[
              ['Path', String(a.workspace_path ?? '—')],
              [
                'Color',
                <span className="inline-flex items-center gap-1.5">
                  <span className="inline-block size-3.5 rounded-sm border border-border" style={{ background: color || '#888' }} />
                  <span>{color || '—'}</span>
                </span>,
              ],
              ['Hidden', a.hidden ? 'yes' : 'no'],
            ]}
          />
        </InfoCard>
      </div>
    </div>
  )
}

const sessionColumns: ColumnDef<Dict, unknown>[] = [
  { accessorKey: 'sid', header: 'Session', cell: (c) => <span className="font-mono text-xs text-primary">{shortId(c.getValue() as string, 18)}</span> },
  { accessorKey: 'size', header: 'Size', cell: (c) => <span className="font-mono text-xs tabular-nums text-muted-foreground">{fmtBytes(c.getValue() as number)}</span> },
  { accessorKey: 'mtime', header: 'Updated', cell: (c) => <span className="text-xs text-muted-foreground">{relativeTime(c.getValue() as number)}</span> },
]

function SessionsTab({ agentId }: { agentId: string }) {
  const q = useAgentSessions(agentId)
  const [active, setActive] = useState<string | null>(null)
  const rows = (q.data?.sessions ?? []) as unknown as Dict[]
  return (
    <>
      <QueryState query={q} isEmpty={() => rows.length === 0}
        empty={<EmptyState title="No sessions for this agent yet" />}>
        {() => (
          <DataTable columns={sessionColumns} data={rows} searchable searchPlaceholder="Search sessions…"
            onRowClick={(r) => setActive(String(r.sid))} emptyTitle="No sessions" />
        )}
      </QueryState>
      <RunReplayDrawer agentId={agentId} sid={active} open={!!active} onOpenChange={(o) => !o && setActive(null)} />
    </>
  )
}

function LlmTab({ agentId }: { agentId: string }) {
  const q = useAgentTraces(agentId)
  const traces = q.data?.traces ?? []
  return <TraceTable traces={traces} />
}

function SkillsTab({ agentId }: { agentId: string }) {
  const q = useAgentCapabilities(agentId)
  const skills = (q.data?.items ?? []).filter((i) => i.kind === 'skill')
  return (
    <QueryState query={q} isEmpty={() => skills.length === 0}
      empty={<EmptyState title="No skills" description="No skill loaded from any of the four scan roots." />}>
      {() => <CapabilityTable items={skills} searchPlaceholder="Search skills…" emptyTitle="No skills" />}
    </QueryState>
  )
}

function ToolsTab({ agentId }: { agentId: string }) {
  const q = useAgentTools(agentId)
  const caps = useAgentCapabilities(agentId)
  const tools = (q.data?.tools ?? []) as Dict[]
  const allow = q.data?.allowlist ?? []
  const deny = q.data?.denylist ?? []
  const policyLabel = deny.length ? `deny ${deny.length}` : allow.length ? `allow ${allow.length}` : 'allow-all'
  const capTools = (caps.data?.items ?? []).filter((i) => i.kind === 'tool')
  return (
    <QueryState query={q} isEmpty={() => tools.length === 0}
      empty={<EmptyState title="No tools registered" />}>
      {() => (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatCard label="Registered" value={tools.length} />
            <StatCard label="Policy" value={policyLabel} />
          </div>
          <ToolsTable tools={tools} />
          <Section title="Capability tools — loader verdicts">
            <QueryState
              query={caps}
              isEmpty={() => capTools.length === 0}
              empty={<p className="text-xs text-muted-foreground">No capability tools scanned across the four roots.</p>}
            >
              {() => (
                <CapabilityTable items={capTools} searchPlaceholder="Search capability tools…" emptyTitle="No capability tools" />
              )}
            </QueryState>
          </Section>
        </div>
      )}
    </QueryState>
  )
}

function PolicyTab({ agentId }: { agentId: string }) {
  const policy = useAgentPolicy(agentId)
  const stats = useAgentPolicyStats(agentId)
  const config = useAgentConfig(agentId)
  const bullets = policy.data?.bullets ?? []
  const cfg = (config.data?.config ?? {}) as Dict
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard label="Total" value={stats.data?.total ?? 0} />
        <StatCard label="Active" value={stats.data?.active ?? 0} />
        <StatCard label="Retired" value={stats.data?.retired ?? 0} />
        <StatCard label="Avg score" value={(stats.data?.avg_score ?? 0).toFixed(2)} />
      </div>

      {bullets.length > 0 && (
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
          <ScoreDistribution bullets={bullets} />
          <TopPerformers bullets={bullets} />
        </div>
      )}

      <PolicyConfigCards config={cfg} />
      <SystemPolicyRules config={cfg} />

      <QueryState query={policy} isEmpty={() => bullets.length === 0}
        empty={<EmptyState title="No policy bullets" />}>
        {() => (
          <Section title="Active bullets">
            <div className="space-y-2">{bullets.map((b, i) => <PolicyBulletCard key={i} bullet={b} />)}</div>
          </Section>
        )}
      </QueryState>

      {policy.data?.raw && (
        <Section title="Policy document">
          <div className="rounded-xl border border-border bg-card p-4">
            <Markdown>{policy.data.raw}</Markdown>
          </div>
        </Section>
      )}
    </div>
  )
}

const TAB_RENDER: Record<TabId, (agentId: string) => ReactNode> = {
  overview: (id) => <OverviewTab agentId={id} />,
  identity: (id) => <IdentityTab agentId={id} />,
  sessions: (id) => <SessionsTab agentId={id} />,
  llm: (id) => <LlmTab agentId={id} />,
  skills: (id) => <SkillsTab agentId={id} />,
  tools: (id) => <ToolsTab agentId={id} />,
  policy: (id) => <PolicyTab agentId={id} />,
  workspace: (id) => <FileTree agentId={id} root="workspace" rootLabel="workspace" />,
  files: (id) => <FileTree agentId={id} root="agent" rootLabel="agent root" />,
}

const TAB_LABEL: Record<TabId, string> = {
  overview: 'Overview', identity: 'Identity', sessions: 'Sessions', llm: 'LLM', skills: 'Skills',
  tools: 'Tools', policy: 'Policy', workspace: 'Workspace', files: 'Files',
}

export function AgentDetailPage() {
  const { id = '', tab } = useParams()
  const navigate = useNavigate()
  const current: TabId = (TABS.includes(tab as TabId) ? tab : 'overview') as TabId
  const agent = useAgent(id)

  const a = agent.data ?? {}
  const label = String(a.display_name || a.name || id)

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b border-border px-6 py-4">
        <button
          type="button"
          onClick={() => navigate('/agents')}
          className="flex size-8 cursor-pointer items-center justify-center rounded-lg border border-border text-muted-foreground hover:bg-muted/50 hover:text-foreground"
          aria-label="Back to agents"
        >
          <ArrowLeft className="size-4" />
        </button>
        <span
          className="flex size-9 items-center justify-center rounded-lg text-sm font-semibold text-primary-foreground"
          style={{ background: (a.color as string) || 'var(--primary)' }}
        >
          {initials(label)}
        </span>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-lg font-semibold tracking-tight text-foreground">{label}</h1>
          {a.did != null && <div className="truncate font-mono text-xs text-muted-foreground">{String(a.did)}</div>}
        </div>
        <StatusDot online={Boolean(a.online)} />
      </div>

      <Tabs value={current} onValueChange={(v) => navigate(`/agents/${id}/${v}`)} className="border-b border-border px-6">
        <TabsList className="my-2 flex-wrap">
          {TABS.map((t) => (
            <TabsTrigger key={t} value={t}>{TAB_LABEL[t]}</TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      <div className="flex-1 overflow-auto p-6">{TAB_RENDER[current](id)}</div>
    </div>
  )
}
