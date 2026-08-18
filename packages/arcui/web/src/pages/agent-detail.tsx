import { useState, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Plus } from 'lucide-react'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Input } from '@/components/ui/input'
import { FilterPills } from '@/components/filter-pills'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { CreateTaskSheet } from '@/components/create-task-sheet'
import { fmtSeconds, isBlocked } from '@/lib/tasks'
import { Button } from '@/components/ui/button'
import { apiPost } from '@/lib/api'
import { RestartGatewayButton } from '@/components/restart-gateway-button'
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
import { SkillDrawer } from '@/components/skill-drawer'
import { PromptDrawer } from '@/components/prompt-drawer'
import { RubricEditor } from '@/components/rubric-editor'
import { isRubricPrompt } from '@/lib/rubric'
import { ToolDrawer } from '@/components/tool-drawer'
import { ScheduleDrawer } from '@/components/schedule-drawer'
import { scheduleTiming, scheduleTitle } from '@/lib/schedule-format'
import { TaskBoard } from '@/components/task-board'
import { TaskDrawer } from '@/components/task-drawer'
import { AreaSeries } from '@/components/charts'
import { QueryState, EmptyState } from '@/components/states'
import {
  useAgent,
  useAgentCapabilities,
  useAgentConfig,
  useAgentPolicy,
  useAgentPolicyStats,
  useAgentPrompts,
  useAgentSchedules,
  useAgentSessions,
  useAgentStats,
  useAgentTasks,
  useAgentConnectors,
  useAgentTimeseries,
  useAgentTools,
  useAgentTraces,
  useApprovals,
  useGatedCapabilities,
  useRoster,
} from '@/lib/queries'
import { useQueryClient } from '@tanstack/react-query'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import type { MentionHandle } from '@/components/mention-composer'
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
import type { CapabilityInventoryItem, Dict, Task, TaskStatus, TaskPriority } from '@/lib/types'

const TASK_STATUS_FILTERS: (TaskStatus | 'all')[] = [
  'all', 'backlog', 'todo', 'in_progress', 'review', 'done', 'failed',
]
const TASK_PRIORITY_FILTERS: (TaskPriority | 'all')[] = ['all', 'low', 'medium', 'high', 'critical']

const TABS = [
  'overview',
  'identity',
  'sessions',
  'llm',
  'skills',
  'tools',
  'prompts',
  'tasks',
  'schedules',
  'policy',
  'trust',
  'workspace',
  'files',
  'connect',
] as const
type TabId = (typeof TABS)[number]

// --- Small building blocks -------------------------------------------------

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
        {title}
      </h3>
      {children}
    </section>
  )
}

/** Titled card used across the detail tabs. */
function InfoCard({
  title,
  extra,
  children,
}: {
  title: string
  extra?: ReactNode
  children: ReactNode
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-4 shadow-xs">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold tracking-tight text-foreground">{title}</h3>
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
        <div
          key={i}
          className="flex items-center justify-between gap-3 py-1.5 text-sm first:pt-0 last:pb-0"
        >
          <dt className="shrink-0 text-[11px] uppercase tracking-[0.06em] text-muted-foreground">
            {k}
          </dt>
          <dd className="min-w-0 truncate text-right font-mono tabular-nums text-foreground">
            {v ?? '—'}
          </dd>
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
  const bg =
    tone === 'error'
      ? 'bg-status-error'
      : tone === 'warning'
        ? 'bg-status-warning'
        : 'bg-status-online'
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
      <div className={cn('h-full rounded-full transition-all', bg)} style={{ width: `${w}%` }} />
    </div>
  )
}

function MetricTile({
  label,
  value,
  pct,
  tone,
}: {
  label: string
  value: string
  pct: number
  tone?: 'online' | 'warning' | 'error'
}) {
  return (
    <div className="space-y-1.5 text-center">
      <div className="text-lg font-semibold tabular-nums text-foreground">{value}</div>
      <MiniBar pct={pct} tone={tone} />
      <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
        {label}
      </div>
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
  const ctxTone =
    ctxPct >= emergencyThr * 100 ? 'error' : ctxPct >= compactThr * 100 ? 'warning' : 'online'

  // O2: prompt-cache accounting for the latest trace — None/None means the
  // provider reported no cache fields at all (never touched, not zero-hit).
  const cacheRead = latest?.cache_read_tokens != null ? Number(latest.cache_read_tokens) : null
  const cacheWrite = latest?.cache_write_tokens != null ? Number(latest.cache_write_tokens) : null
  const cacheOutput = latest?.output_tokens != null ? Number(latest.output_tokens) : null
  const cacheHitRate =
    cacheRead != null && used + cacheRead > 0
      ? Math.round((cacheRead / (used + cacheRead)) * 100)
      : null

  const calls = Number(s.request_count ?? 0)
  const errorCount = Number(s.error_count ?? 0)
  const latencyAvg = Math.round(Number(s.latency_avg ?? 0))
  const latencyP95 = Math.round(Number(s.latency_p95 ?? 0))
  const totalCost = Number(s.total_cost ?? 0)
  const successPct = calls > 0 ? Math.round(((calls - errorCount) / calls) * 100) : 0
  const uptimePct = online ? 100 : calls > 0 ? successPct : 0
  const responsePct =
    latencyAvg > 0 ? Math.max(5, Math.min(100, Math.round(100 - (latencyAvg / 5000) * 100))) : 0
  const responseLabel = latencyAvg > 0 ? fmtLatency(latencyAvg) : '—'

  const buckets = ts.data?.buckets ?? []
  const labels = bucketLabels(ts.data?.window ?? '24h', buckets.length)
  const volume = buckets.map((b, i) => ({
    label: labels[i],
    tokens: b.total_tokens,
  }))

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
              extra={
                <Pill tone={online ? 'online' : 'neutral'}>{online ? 'live' : 'offline'}</Pill>
              }
            >
              <KVList
                rows={[
                  ['DID', <span className="text-status-online">{String(a.did ?? '—')}</span>],
                  ['Organization', String(a.org ?? '—')],
                  ['Agent Type', String(a.type ?? '—')],
                  ['Display Name', String(a.display_name ?? a.name ?? '—')],
                  [
                    'Status',
                    <Pill tone={online ? 'online' : 'neutral'}>
                      {online ? 'online' : 'offline'}
                    </Pill>,
                  ],
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
                  [
                    'Tool Timeout',
                    toolsPolicy.timeout_seconds != null ? `${toolsPolicy.timeout_seconds}s` : '—',
                  ],
                  ['Telemetry', tel.enabled ? 'enabled' : '—'],
                  ['Service', String(tel.service_name ?? '—')],
                ]}
              />
            </InfoCard>

            <InfoCard
              title="Context Window"
              extra={<span className="text-xs text-muted-foreground">last prompt</span>}
            >
              <div className="mb-1.5 flex justify-between text-xs text-muted-foreground">
                <span>Last prompt size</span>
                <span className="tabular-nums">{totalCtx ? `${ctxPct}%` : '—'}</span>
              </div>
              <MiniBar pct={ctxPct} tone={ctxTone} />
              <div className="mt-3 grid grid-cols-3 gap-3 text-sm">
                <div>
                  <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                    Used
                  </div>
                  <div className="font-semibold tabular-nums text-foreground">
                    {totalCtx ? fmtNumber(used) : '—'}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                    Available
                  </div>
                  <div className="font-semibold tabular-nums text-foreground">
                    {totalCtx ? fmtNumber(available) : '—'}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                    Total
                  </div>
                  <div className="font-semibold tabular-nums text-foreground">
                    {totalCtx ? fmtNumber(totalCtx) : '—'}
                  </div>
                </div>
              </div>
              <div className="mt-3 flex flex-wrap gap-3 rounded-lg border border-border/60 bg-muted/40 p-2 text-xs">
                <span className="text-muted-foreground">Thresholds</span>
                <span>
                  <span className="text-status-online">●</span> Prune {Math.round(pruneThr * 100)}%
                </span>
                <span>
                  <span className="text-status-warning">●</span> Compact{' '}
                  {Math.round(compactThr * 100)}%
                </span>
                <span>
                  <span className="text-status-error">●</span> Emergency{' '}
                  {Math.round(emergencyThr * 100)}%
                </span>
              </div>
              <div className="mt-2 flex flex-wrap gap-3 rounded-lg border border-border/60 bg-muted/40 p-2 text-xs">
                <span className="text-muted-foreground">Cache</span>
                {cacheRead == null && cacheWrite == null ? (
                  <span className="text-muted-foreground">—</span>
                ) : (
                  <>
                    <span>
                      Input <span className="font-mono text-foreground">{fmtNumber(used)}</span>
                    </span>
                    <span>
                      Read{' '}
                      <span className="font-mono text-foreground">
                        {cacheRead != null ? fmtNumber(cacheRead) : '—'}
                      </span>
                    </span>
                    <span>
                      Write{' '}
                      <span className="font-mono text-foreground">
                        {cacheWrite != null ? fmtNumber(cacheWrite) : '—'}
                      </span>
                    </span>
                    <span>
                      Output{' '}
                      <span className="font-mono text-foreground">
                        {cacheOutput != null ? fmtNumber(cacheOutput) : '—'}
                      </span>
                    </span>
                    <span>
                      Hit rate{' '}
                      <span className="font-mono text-foreground">
                        {cacheHitRate != null ? `${cacheHitRate}%` : '—'}
                      </span>
                    </span>
                  </>
                )}
              </div>
            </InfoCard>
          </div>

          {/* Right column */}
          <div className="space-y-4">
            <InfoCard title="Performance (24h)">
              <div className="grid grid-cols-3 gap-4">
                <MetricTile label="Uptime" value={`${uptimePct}%`} pct={uptimePct} />
                <MetricTile label="Avg Response" value={responseLabel} pct={responsePct} />
                <MetricTile
                  label="Tool Success"
                  value={`${successPct}%`}
                  pct={successPct}
                  tone={successPct >= 90 ? 'online' : successPct >= 70 ? 'warning' : 'error'}
                />
              </div>
              <div className="mt-4">
                <div className="mb-1 text-xs text-muted-foreground">Token usage (24h)</div>
                {volume.some((v) => v.tokens > 0) ? (
                  <div className="h-32">
                    <AreaSeries data={volume} dataKey="tokens" />
                  </div>
                ) : (
                  <div className="flex h-20 items-center justify-center rounded-lg border border-border/60 bg-muted/40 text-xs text-muted-foreground">
                    No 24h activity
                  </div>
                )}
              </div>
              <div className="mt-3 text-xs text-muted-foreground">
                Total cost <span className="text-foreground">{fmtCost(totalCost)}</span> · P95{' '}
                <span className="font-mono text-foreground">{latencyP95}ms</span> · Calls{' '}
                <span className="font-mono text-foreground">{fmtNumber(calls)}</span>
              </div>
            </InfoCard>

            <InfoCard
              title={`Recent Sessions${sessionRows.length ? ` (${sessionRows.length})` : ''}`}
            >
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
                      <span className="truncate font-mono text-xs text-primary">
                        {shortId(sess.sid, 22)}
                      </span>
                      <span className="shrink-0 text-xs text-muted-foreground">
                        {relativeTime(sess.mtime)}
                      </span>
                      <span className="shrink-0 font-mono text-xs text-muted-foreground">
                        {fmtBytes(sess.size)}
                      </span>
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
                      <span className="min-w-0 truncate text-foreground">
                        {String(t.title ?? t.id ?? 'task')}
                      </span>
                      {t.status != null && (
                        <span className="shrink-0 rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                          {String(t.status)}
                        </span>
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
                        <span className="min-w-0 truncate text-foreground">
                          {String(sched.name ?? sched.id ?? 'schedule')}
                        </span>
                        {(sched.cron ?? sched.schedule ?? sched.next_run) != null && (
                          <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
                            {String(sched.cron ?? sched.schedule ?? sched.next_run)}
                          </span>
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
          <code className="block break-all rounded-lg border border-border bg-muted/40 p-3 font-mono text-sm text-status-online">
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
              [
                'Timeout',
                toolsPolicy.timeout_seconds != null ? `${toolsPolicy.timeout_seconds}s` : '—',
              ],
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
                  <span
                    className="inline-block size-3.5 rounded-sm border border-border"
                    style={{ background: color || '#888' }}
                  />
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
  {
    accessorKey: 'sid',
    header: 'Session',
    cell: (c) => (
      <span className="font-mono text-xs text-primary">{shortId(c.getValue() as string, 18)}</span>
    ),
  },
  {
    accessorKey: 'size',
    header: 'Size',
    cell: (c) => (
      <span className="font-mono text-xs tabular-nums text-muted-foreground">
        {fmtBytes(c.getValue() as number)}
      </span>
    ),
  },
  {
    accessorKey: 'mtime',
    header: 'Updated',
    cell: (c) => (
      <span className="text-xs text-muted-foreground">{relativeTime(c.getValue() as number)}</span>
    ),
  },
]

function SessionsTab({ agentId }: { agentId: string }) {
  const q = useAgentSessions(agentId)
  const [active, setActive] = useState<string | null>(null)
  const rows = (q.data?.sessions ?? []) as unknown as Dict[]
  return (
    <>
      <QueryState
        query={q}
        isEmpty={() => rows.length === 0}
        empty={<EmptyState title="No sessions for this agent yet" />}
      >
        {() => (
          <DataTable
            columns={sessionColumns}
            data={rows}
            searchable
            searchPlaceholder="Search sessions…"
            onRowClick={(r) => setActive(String(r.sid))}
            emptyTitle="No sessions"
          />
        )}
      </QueryState>
      <RunReplayDrawer
        agentId={agentId}
        sid={active}
        open={!!active}
        onOpenChange={(o) => !o && setActive(null)}
      />
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
  const [selected, setSelected] = useState<string | null>(null)
  return (
    <>
      <QueryState
        query={q}
        isEmpty={() => skills.length === 0}
        empty={
          <EmptyState
            title="No skills"
            description="No skill loaded from any of the four scan roots."
          />
        }
      >
        {() => (
          <CapabilityTable
            items={skills}
            searchPlaceholder="Search skills…"
            emptyTitle="No skills"
            onRowClick={(item: CapabilityInventoryItem) => setSelected(item.name)}
          />
        )}
      </QueryState>
      <SkillDrawer
        agentId={agentId}
        skillName={selected}
        open={selected != null}
        onOpenChange={(o) => !o && setSelected(null)}
      />
    </>
  )
}

function ToolsTab({ agentId }: { agentId: string }) {
  const q = useAgentTools(agentId)
  const caps = useAgentCapabilities(agentId)
  const tools = (q.data?.tools ?? []) as Dict[]
  const allow = q.data?.allowlist ?? []
  const deny = q.data?.denylist ?? []
  const policyLabel = deny.length
    ? `deny ${deny.length}`
    : allow.length
      ? `allow ${allow.length}`
      : 'allow-all'
  const capTools = (caps.data?.items ?? []).filter((i) => i.kind === 'tool')
  const [selected, setSelected] = useState<string | null>(null)
  return (
    <>
      <QueryState
        query={q}
        isEmpty={() => tools.length === 0}
        empty={<EmptyState title="No tools registered" />}
      >
        {() => (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <StatCard label="Registered" value={tools.length} />
              <StatCard label="Policy" value={policyLabel} />
            </div>
            <ToolsTable tools={tools} onRowClick={(t) => setSelected(String(t.name))} />
            <Section title="Capability tools — loader verdicts">
              <QueryState
                query={caps}
                isEmpty={() => capTools.length === 0}
                empty={
                  <p className="text-xs text-muted-foreground">
                    No capability tools scanned across the four roots.
                  </p>
                }
              >
                {() => (
                  <CapabilityTable
                    items={capTools}
                    searchPlaceholder="Search capability tools…"
                    emptyTitle="No capability tools"
                    onRowClick={(item: CapabilityInventoryItem) => setSelected(item.name)}
                  />
                )}
              </QueryState>
            </Section>
          </div>
        )}
      </QueryState>
      <ToolDrawer
        agentId={agentId}
        toolName={selected}
        open={selected != null}
        onOpenChange={(o) => !o && setSelected(null)}
      />
    </>
  )
}

function TasksTab({ agentId }: { agentId: string }) {
  const q = useAgentTasks(agentId)
  const roster = useRoster()
  const [operatorMode] = useOperatorMode()
  const [selected, setSelected] = useState<Task | null>(null)
  const [creating, setCreating] = useState(false)
  const [statusFilter, setStatusFilter] = useState<TaskStatus | 'all'>('all')
  const [priorityFilter, setPriorityFilter] = useState<TaskPriority | 'all'>('all')
  const [tagFilter, setTagFilter] = useState('all')

  const tasks = q.data?.tasks ?? []
  const agents = roster.data?.agents ?? []
  const thisDid = agents.find((a) => a.agent_id === agentId)?.did ?? ''
  const byDid = new Map(agents.filter((a) => a.did).map((a) => [a.did as string, a]))
  const resolveOwner = (ownerDid: string | null | undefined): string | null => {
    if (!ownerDid) return null
    const a = byDid.get(ownerDid)
    return a ? String(a.display_name || a.name || ownerDid) : ownerDid
  }
  const mentionHandles: MentionHandle[] = agents
    .map((a) => ({
      handle: String(a.name || a.agent_id || ''),
      label: String(a.display_name || a.name || a.agent_id || ''),
      color: typeof a.color === 'string' ? a.color : undefined,
    }))
    .filter((h) => h.handle)

  const statusById = new Map<string, string>()
  for (const t of tasks) if (t.id) statusById.set(t.id, t.status ?? 'backlog')

  const statusCounts: Record<string, number> = { all: tasks.length }
  for (const t of tasks) {
    const k = t.status ?? 'backlog'
    statusCounts[k] = (statusCounts[k] ?? 0) + 1
  }
  const priorityCounts: Record<string, number> = { all: tasks.length }
  for (const t of tasks) {
    const k = t.priority ?? 'medium'
    priorityCounts[k] = (priorityCounts[k] ?? 0) + 1
  }
  const tags = [...new Set(tasks.flatMap((t) => t.tags ?? []))].sort()

  const today = new Date().toISOString().slice(0, 10)
  let inProgress = 0
  let doneToday = 0
  let failed = 0
  let blocked = 0
  const doneDurations: number[] = []
  for (const t of tasks) {
    if (t.status === 'in_progress') inProgress++
    if (t.status === 'failed') failed++
    if (isBlocked(t, statusById)) blocked++
    if (t.status === 'done') {
      const completed = t.completed_at ?? t.updated_at
      if (completed?.slice(0, 10) === today) doneToday++
      if (t.duration_seconds != null) doneDurations.push(t.duration_seconds)
      else if (t.started_at && t.completed_at)
        doneDurations.push((Date.parse(t.completed_at) - Date.parse(t.started_at)) / 1000)
    }
  }
  const avgDone = doneDurations.length
    ? doneDurations.reduce((a, b) => a + b, 0) / doneDurations.length
    : null

  const boardTasks = tasks.filter(
    (t) =>
      (priorityFilter === 'all' || t.priority === priorityFilter) &&
      (tagFilter === 'all' || (t.tags ?? []).includes(tagFilter)),
  )

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
          <span className="tabular-nums">{tasks.length} tasks</span>
          <span className="text-border">·</span>
          <span className="tabular-nums">{blocked} blocked</span>
        </div>
        {operatorMode && (
          <Button size="sm" onClick={() => setCreating(true)}>
            <Plus className="size-3.5" /> New task
          </Button>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard label="In progress" value={inProgress} />
        <StatCard label="Done today" value={doneToday} />
        <StatCard label="Avg time to done" value={avgDone != null ? fmtSeconds(avgDone) : '—'} />
        <StatCard label="Failed" value={failed} />
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <FilterPills
          value={statusFilter}
          onChange={(v) => setStatusFilter(v as TaskStatus | 'all')}
          options={TASK_STATUS_FILTERS.map((s) => ({
            value: s,
            label: s === 'all' ? 'All' : s.replace(/_/g, ' '),
            count: statusCounts[s] ?? 0,
          }))}
        />
        <FilterPills
          value={priorityFilter}
          onChange={(v) => setPriorityFilter(v as TaskPriority | 'all')}
          options={TASK_PRIORITY_FILTERS.map((p) => ({
            value: p,
            label: p === 'all' ? 'All priority' : p,
            count: priorityCounts[p] ?? 0,
          }))}
        />
        {tags.length > 0 && (
          <Select value={tagFilter} onValueChange={setTagFilter}>
            <SelectTrigger size="sm">
              <SelectValue placeholder="Tag" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All tags</SelectItem>
              {tags.map((tag) => (
                <SelectItem key={tag} value={tag}>
                  {tag}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </div>

      <QueryState
        query={q}
        isEmpty={() => tasks.length === 0}
        empty={<EmptyState title="No tasks" description="This agent has no owned tasks." />}
      >
        {() => (
          <TaskBoard
            tasks={boardTasks}
            resolveOwner={resolveOwner}
            onSelectTask={setSelected}
            focusStatus={statusFilter}
          />
        )}
      </QueryState>

      <TaskDrawer
        task={selected}
        open={selected != null}
        onOpenChange={(o) => !o && setSelected(null)}
        operatorMode={operatorMode}
        roster={agents}
        mentionHandles={mentionHandles}
        allTasks={tasks}
      />
      <CreateTaskSheet
        open={creating}
        onOpenChange={setCreating}
        roster={agents}
        defaultOwnerDid={thisDid}
      />
    </div>
  )
}

const scheduleColumns: ColumnDef<Dict, unknown>[] = [
  {
    id: 'title',
    header: 'Schedule',
    accessorFn: (r) => scheduleTitle(r),
    cell: (c) => <span className="text-xs text-foreground">{c.getValue() as string}</span>,
  },
  {
    accessorKey: 'type',
    header: 'Type',
    cell: (c) => <span className="text-xs text-muted-foreground">{String(c.getValue())}</span>,
  },
  {
    id: 'timing',
    header: 'Runs',
    accessorFn: (r) => scheduleTiming(r),
    cell: (c) => <span className="text-xs text-muted-foreground">{c.getValue() as string}</span>,
  },
  {
    accessorKey: 'enabled',
    header: 'Enabled',
    cell: (c) => (
      <span className="text-xs text-foreground">{c.getValue() === false ? 'no' : 'yes'}</span>
    ),
  },
]

function SchedulesTab({ agentId }: { agentId: string }) {
  const q = useAgentSchedules(agentId)
  const rows = (q.data?.schedules ?? []) as Dict[]
  const [selected, setSelected] = useState<Dict | null>(null)
  const [operatorMode] = useOperatorMode()
  return (
    <>
      <QueryState
        query={q}
        isEmpty={() => rows.length === 0}
        empty={<EmptyState title="No schedules" description="This agent has no scheduled tasks." />}
      >
        {() => (
          <DataTable
            columns={scheduleColumns}
            data={rows}
            searchable
            searchPlaceholder="Search schedules…"
            onRowClick={(r) => setSelected(r)}
            emptyTitle="No schedules"
          />
        )}
      </QueryState>
      <ScheduleDrawer
        schedule={selected}
        open={selected != null}
        onOpenChange={(o) => !o && setSelected(null)}
        agentId={agentId}
        operatorMode={operatorMode}
      />
    </>
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

      <QueryState
        query={policy}
        isEmpty={() => bullets.length === 0}
        empty={<EmptyState title="No policy bullets" />}
      >
        {() => (
          <Section title="Active bullets">
            <div className="space-y-2">
              {bullets.map((b, i) => (
                <PolicyBulletCard key={i} bullet={b} />
              ))}
            </div>
          </Section>
        )}
      </QueryState>

      {policy.data?.raw && (
        <Section title="Policy document">
          <div className="rounded-lg border border-border bg-card p-4 shadow-xs">
            <Markdown>{policy.data.raw}</Markdown>
          </div>
        </Section>
      )}
    </div>
  )
}

function PromptsTab({ agentId }: { agentId: string }) {
  const q = useAgentPrompts(agentId)
  const items = q.data?.items ?? []
  const [selected, setSelected] = useState<{
    package: string
    name: string
  } | null>(null)

  const byPackage = new Map<string, typeof items>()
  for (const item of items) {
    const list = byPackage.get(item.package) ?? []
    list.push(item)
    byPackage.set(item.package, list)
  }

  return (
    <>
      <QueryState
        query={q}
        isEmpty={() => items.length === 0}
        empty={
          <EmptyState
            title="No prompts"
            description="No stock prompts found across installed packages."
          />
        }
      >
        {() => (
          <div className="space-y-6">
            {[...byPackage.entries()].map(([pkg, prompts]) => (
              <Section key={pkg} title={pkg}>
                <div className="divide-y divide-border rounded-md border border-border">
                  {prompts.map((p) => (
                    <button
                      key={p.name}
                      type="button"
                      onClick={() => setSelected({ package: p.package, name: p.name })}
                      className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                    >
                      <div className="min-w-0">
                        <div className="truncate font-mono text-xs text-foreground">{p.name}</div>
                        <div className="truncate text-[11px] text-muted-foreground">
                          {p.description}
                        </div>
                      </div>
                      <span
                        className={cn(
                          'shrink-0 rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide',
                          p.status === 'overridden'
                            ? 'border-primary/40 bg-primary/10 text-primary'
                            : 'border-border bg-muted/40 text-muted-foreground',
                        )}
                      >
                        {p.status}
                      </span>
                    </button>
                  ))}
                </div>
              </Section>
            ))}
          </div>
        )}
      </QueryState>
      <PromptDrawer
        agentId={agentId}
        prompt={isRubricPrompt(selected) ? null : selected}
        open={selected != null && !isRubricPrompt(selected)}
        onOpenChange={(o) => !o && setSelected(null)}
      />
      <RubricEditor
        agentId={agentId}
        prompt={isRubricPrompt(selected) ? selected : null}
        open={selected != null && isRubricPrompt(selected)}
        onOpenChange={(o) => !o && setSelected(null)}
      />
    </>
  )
}

/**
 * What this one agent can actually reach.
 *
 * Read from its own side — the grants naming it — because that is the set its
 * connector module attaches at startup. An agent showing every connection the
 * deployment has would be the exact confusion deny-by-default exists to remove:
 * the account is connected and this agent still cannot use it. Granting happens
 * on the Connections page, where every agent is visible at once.
 */
function AgentReachCard({ agentId }: { agentId: string }) {
  const reach = useAgentConnectors(agentId)
  const held = reach.data?.instances ?? []

  return (
    <InfoCard title="Can reach">
      {held.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          Nothing yet. Connected accounts are granted on the{' '}
          <Link to="/connections" className="underline underline-offset-2">
            Connections
          </Link>{' '}
          page — an agent only gets what it is granted.
        </p>
      ) : (
        <ul className="space-y-1.5">
          {held.map((c) => (
            <li key={c.instance} className="flex flex-wrap items-baseline gap-x-2 text-sm">
              <span className="font-medium text-foreground">{c.instance}</span>
              <span className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                {c.extension_display_name}
              </span>
              <span className="text-xs text-muted-foreground">approval: {c.approval}</span>
            </li>
          ))}
        </ul>
      )}
    </InfoCard>
  )
}

function ConnectTab({ agentId }: { agentId: string }) {
  const [operatorMode] = useOperatorMode()
  const [token, setToken] = useState('')
  const [userId, setUserId] = useState('')
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    setBusy(true)
    setError(null)
    setDone(null)
    try {
      const uid = Number(userId.trim())
      if (!Number.isInteger(uid)) throw new Error('Your Telegram user ID must be a number.')
      const res = await apiPost<{ message?: string }>(`/api/agents/${agentId}/connect-telegram`, {
        token: token.trim(),
        user_id: uid,
      })
      setDone(res.message ?? 'Connected.')
      setToken('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to connect Telegram')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="max-w-xl space-y-4">
      <AgentReachCard agentId={agentId} />
      <InfoCard title="Connect Telegram">
        {!operatorMode && (
          <p className="mb-3 rounded-md border border-border bg-muted/40 p-2 text-sm text-muted-foreground">
            Turn on operator mode (top-right) to connect a bot.
          </p>
        )}
        <p className="mb-4 text-sm text-muted-foreground">
          Create a bot with <span className="font-mono">@BotFather</span> (
          <span className="font-mono">/newbot</span>), paste its token below, and add your Telegram
          user ID (message <span className="font-mono">@userinfobot</span> to get it). The token is
          stored securely on the server and never shown again.
        </p>
        <div className="space-y-3">
          <div>
            <label className="mb-1 block text-[11px] uppercase tracking-[0.06em] text-muted-foreground">
              Bot token
            </label>
            <Input
              type="password"
              autoComplete="off"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="8012345678:AA…"
              disabled={!operatorMode || busy}
            />
          </div>
          <div>
            <label className="mb-1 block text-[11px] uppercase tracking-[0.06em] text-muted-foreground">
              Your Telegram user ID
            </label>
            <Input
              value={userId}
              onChange={(e) => setUserId(e.target.value)}
              placeholder="8293394811"
              disabled={!operatorMode || busy}
            />
          </div>
          <Button
            onClick={submit}
            disabled={!operatorMode || busy || !token.trim() || !userId.trim()}
          >
            {busy ? 'Connecting…' : 'Connect'}
          </Button>
          {done && (
            <p className="text-sm text-foreground">
              {done}{' '}
              <span className="text-muted-foreground">
                Restart the gateway to bring the bot live.
              </span>
            </p>
          )}
          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>
        <div className="mt-4 flex items-center justify-between gap-3 border-t border-border pt-3">
          <span className="text-xs text-muted-foreground">
            New bots go live on the next gateway restart.
          </span>
          <RestartGatewayButton />
        </div>
      </InfoCard>
    </div>
  )
}

/**
 * Governance scoped to one agent — its pending trifecta approvals (actionable)
 * and its quarantined capabilities. The fleet-wide screens still exist; this is
 * the same data filtered to the agent you are looking at (GAP-3 / GAP-4).
 */
function TrustTab({ agentId }: { agentId: string }) {
  const agent = useAgent(agentId)
  const did = String(agent.data?.did ?? '')
  const approvalsQ = useApprovals()
  const gatedQ = useGatedCapabilities(false)
  const [operatorMode] = useOperatorMode()
  const queryClient = useQueryClient()

  const approvals = (approvalsQ.data?.approvals ?? []).filter((a) => a.agent_did === did)
  const gated = (gatedQ.data?.gated ?? []).filter((g) => g.agent_id === agentId)

  const act = async (id: string, decision: 'approve' | 'deny') => {
    try {
      await apiPost(`/api/approvals/${encodeURIComponent(id)}/${decision}`)
    } finally {
      queryClient.invalidateQueries({ queryKey: ['approvals'] })
    }
  }

  return (
    <div className="space-y-6">
      <Section title={`Pending approvals (${approvals.length})`}>
        {approvals.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing is waiting on you for this agent.</p>
        ) : (
          <div className="space-y-2">
            {approvals.map((a) => (
              <div
                key={a.id}
                className="flex items-center gap-3 rounded-lg border border-border bg-card p-3.5"
              >
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-semibold text-foreground">
                    Wants to run <span className="font-mono">{a.tool}</span>
                  </div>
                  <div className="truncate text-xs text-muted-foreground">
                    {a.legs?.length ? a.legs.join(', ') : 'trifecta gate'} · {relativeTime(a.created_at)}
                  </div>
                </div>
                {operatorMode ? (
                  <div className="flex shrink-0 gap-2">
                    <Button size="sm" onClick={() => act(a.id, 'approve')}>
                      Approve
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => act(a.id, 'deny')}>
                      Deny
                    </Button>
                  </div>
                ) : (
                  <Link
                    to="/approvals"
                    className="shrink-0 rounded-md border border-border bg-secondary px-3 py-1.5 text-xs font-semibold text-secondary-foreground hover:border-foreground/20"
                  >
                    Review
                  </Link>
                )}
              </div>
            ))}
          </div>
        )}
      </Section>

      <Section title={`Pending capabilities (${gated.length})`}>
        {gated.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No quarantined tools or skills for this agent.
          </p>
        ) : (
          <div className="space-y-2">
            {gated.map((g) => (
              <div
                key={`${g.kind}-${g.name}`}
                className="flex items-center gap-3 rounded-lg border border-border bg-card p-3.5"
              >
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-semibold text-foreground">
                    {g.name}{' '}
                    <span className="text-xs font-normal text-muted-foreground">({g.kind})</span>
                  </div>
                  <div className="truncate font-mono text-xs text-muted-foreground">
                    {g.status} · {g.hash}
                  </div>
                </div>
                <Link
                  to="/gated"
                  className="shrink-0 rounded-md border border-border bg-secondary px-3 py-1.5 text-xs font-semibold text-secondary-foreground hover:border-foreground/20"
                >
                  Manage
                </Link>
              </div>
            ))}
          </div>
        )}
      </Section>
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
  prompts: (id) => <PromptsTab agentId={id} />,
  tasks: (id) => <TasksTab agentId={id} />,
  schedules: (id) => <SchedulesTab agentId={id} />,
  policy: (id) => <PolicyTab agentId={id} />,
  trust: (id) => <TrustTab agentId={id} />,
  workspace: (id) => <FileTree agentId={id} root="workspace" rootLabel="workspace" />,
  files: (id) => <FileTree agentId={id} root="agent" rootLabel="agent root" />,
  connect: (id) => <ConnectTab agentId={id} />,
}

const TAB_LABEL: Record<TabId, string> = {
  overview: 'Overview',
  identity: 'Identity',
  sessions: 'Sessions',
  llm: 'LLM',
  skills: 'Skills',
  tools: 'Tools',
  prompts: 'Prompts',
  tasks: 'Tasks',
  schedules: 'Schedules',
  policy: 'Policy',
  trust: 'Trust',
  workspace: 'Workspace',
  files: 'Files',
  connect: 'Connect',
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
      <div className="flex items-center gap-3 border-b border-border px-6 py-3.5">
        <button
          type="button"
          onClick={() => navigate('/agents')}
          className="flex size-8 shrink-0 cursor-pointer items-center justify-center rounded-md border border-border text-muted-foreground transition-colors hover:bg-muted/50 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          aria-label="Back to agents"
        >
          <ArrowLeft className="size-4" />
        </button>
        <span
          className="flex size-9 shrink-0 items-center justify-center rounded-lg text-sm font-semibold text-primary-foreground"
          style={{ background: (a.color as string) || 'var(--primary)' }}
        >
          {initials(label)}
        </span>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-lg font-semibold tracking-tight text-foreground">{label}</h1>
          {a.did != null && (
            <span className="mt-1 inline-block max-w-full truncate rounded border border-border bg-muted/40 px-1.5 py-0.5 align-middle font-mono text-[11px] text-muted-foreground">
              {String(a.did)}
            </span>
          )}
        </div>
        <StatusDot online={Boolean(a.online)} />
      </div>

      <Tabs
        value={current}
        onValueChange={(v) => navigate(`/agents/${id}/${v}`)}
        className="border-b border-border px-6"
      >
        <TabsList className="my-2 h-auto flex-wrap">
          {TABS.map((t) => (
            <TabsTrigger key={t} value={t}>
              {TAB_LABEL[t]}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      <div className="flex-1 overflow-auto p-6">{TAB_RENDER[current](id)}</div>
    </div>
  )
}
