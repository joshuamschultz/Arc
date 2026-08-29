import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { motion } from 'motion/react'
import { ArrowLeft, Plus, FileText, Pencil, Mail } from 'lucide-react'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { FilterPills } from '@/components/filter-pills'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { CreateTaskSheet } from '@/components/create-task-sheet'
import { fmtSeconds, isBlocked } from '@/lib/tasks'
import { Button } from '@/components/ui/button'
import { ApiError, apiPost } from '@/lib/api'
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
  useAgentChannels,
  useAgentInbox,
  useAgentInboxSearch,
  useAgentInboxThread,
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
  useKnowledge,
  useRuns,
  useRoster,
} from '@/lib/queries'
import { ApprovalRequest } from '@/components/hitl'
import { StatusChip, InsightStat } from '@/components/ai'
import { KnowledgeOverview } from '@/components/knowledge-view/overview'
import { MemoryBrowser } from '@/components/knowledge-memories'
import { EntityBrowser } from '@/components/knowledge-entities'
import { InsightBrowser } from '@/components/knowledge-insights'
import { ProcedureBrowser } from '@/components/knowledge-procedures'
import { EventBrowser } from '@/components/knowledge-events'
import { DailyNotesBrowser } from '@/components/knowledge-daily-notes'
import { RunDetailDrawer } from '@/components/run-detail-drawer'
import { SpawnLineage } from '@/components/run-observability'
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
import type { CapabilityInventoryItem, Dict, Task, TaskStatus, TaskPriority, RunSummary } from '@/lib/types'

const TASK_STATUS_FILTERS: (TaskStatus | 'all')[] = [
  'all', 'backlog', 'todo', 'in_progress', 'review', 'done', 'failed',
]
const TASK_PRIORITY_FILTERS: (TaskPriority | 'all')[] = ['all', 'low', 'medium', 'high', 'critical']

const TABS = [
  'overview',
  'identity',
  'inbox',
  'sessions',
  'runs',
  'llm',
  'skills',
  'tools',
  'prompts',
  'knowledge',
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
  const navigate = useNavigate()
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
                      {sess.current && (
                        <span className="shrink-0 rounded-full bg-primary/15 px-1.5 py-0.5 text-[10px] font-semibold text-primary">
                          Current
                        </span>
                      )}
                      <span className="ml-auto shrink-0 text-xs text-muted-foreground">
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
                    <button
                      key={i}
                      type="button"
                      onClick={() =>
                        navigate(
                          t.id
                            ? `/agents/${agentId}/tasks?open=${encodeURIComponent(String(t.id))}`
                            : `/agents/${agentId}/tasks`,
                        )
                      }
                      className="-mx-1.5 flex w-[calc(100%+0.75rem)] items-center justify-between gap-3 rounded-md px-1.5 py-1 text-left text-sm transition-colors hover:bg-muted/40"
                    >
                      <span className="min-w-0 truncate text-primary hover:underline">
                        {String(t.title ?? t.id ?? 'task')}
                      </span>
                      {t.status != null && (
                        <span className="shrink-0 rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                          {String(t.status)}
                        </span>
                      )}
                    </button>
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
                      <button
                        key={i}
                        type="button"
                        onClick={() =>
                          navigate(
                            sched.id
                              ? `/agents/${agentId}/schedules?open=${encodeURIComponent(String(sched.id))}`
                              : `/agents/${agentId}/schedules`,
                          )
                        }
                        className="-mx-1.5 flex w-[calc(100%+0.75rem)] items-center justify-between gap-3 rounded-md px-1.5 py-1 text-left text-sm transition-colors hover:bg-muted/40"
                      >
                        <span className="min-w-0 truncate text-primary hover:underline">
                          {String(sched.name ?? sched.id ?? 'schedule')}
                        </span>
                        {(sched.cron ?? sched.schedule ?? sched.next_run) != null && (
                          <span className="shrink-0 font-mono text-[11px] text-muted-foreground">
                            {String(sched.cron ?? sched.schedule ?? sched.next_run)}
                          </span>
                        )}
                      </button>
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
      <span className="flex items-center gap-2">
        <span className="font-mono text-xs text-primary">{shortId(c.getValue() as string, 18)}</span>
        {(c.row.original as Dict).current === true && (
          <span className="rounded-full bg-primary/15 px-1.5 py-0.5 text-[10px] font-semibold text-primary">
            Current
          </span>
        )}
      </span>
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
  const [searchParams, setSearchParams] = useSearchParams()

  const tasks = useMemo(() => q.data?.tasks ?? [], [q.data])

  // Deep link from the overview: `?open=<task-id>` opens that task's drawer,
  // then the param is consumed so closing the drawer does not reopen it.
  useEffect(() => {
    const openId = searchParams.get('open')
    if (!openId || tasks.length === 0) return
    const match = tasks.find((t) => t.id === openId)
    // One-time transfer of the `?open=` deep link into drawer state, then the
    // param is consumed below — a legitimate URL→state sync, not a render loop.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (match) setSelected(match)
    searchParams.delete('open')
    setSearchParams(searchParams, { replace: true })
  }, [searchParams, tasks, setSearchParams])
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
  const rows = useMemo(() => (q.data?.schedules ?? []) as Dict[], [q.data])
  const [selected, setSelected] = useState<Dict | null>(null)
  const [operatorMode] = useOperatorMode()
  const [searchParams, setSearchParams] = useSearchParams()

  // Deep link from the overview: `?open=<schedule-id>` opens that schedule's
  // drawer, then the param is consumed so closing it does not reopen it.
  useEffect(() => {
    const openId = searchParams.get('open')
    if (!openId || rows.length === 0) return
    const match = rows.find((r) => String(r.id) === openId)
    // One-time transfer of the `?open=` deep link into drawer state, then the
    // param is consumed below — a legitimate URL→state sync, not a render loop.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (match) setSelected(match)
    searchParams.delete('open')
    setSearchParams(searchParams, { replace: true })
  }, [searchParams, rows, setSearchParams])

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

/**
 * Prompts as a cover-flow carousel — every stock prompt across every package
 * flows into ONE deck. The centered card is enlarged and in focus; click it to
 * open the editor (prose drawer or rubric form). Side cards peek at reduced
 * scale/opacity; click one, drag, scroll, or arrow-key to bring it to center.
 * Mirrors the runs Cover Flow interaction (run-coverflow.tsx).
 */
function PromptsTab({ agentId }: { agentId: string }) {
  const q = useAgentPrompts(agentId)
  const items = q.data?.items ?? []
  const [selected, setSelected] = useState<{
    package: string
    name: string
  } | null>(null)
  const [area, setArea] = useState('all')

  // Prompts grouped by area (package). The filter narrows to one area; "All"
  // shows every area as its own row.
  const areas = [...new Set(items.map((p) => p.package))].sort()
  const shown = area === 'all' ? items : items.filter((p) => p.package === area)
  const byArea = new Map<string, typeof items>()
  for (const p of shown) {
    const list = byArea.get(p.package) ?? []
    list.push(p)
    byArea.set(p.package, list)
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
          <div className="flex flex-col gap-5">
            {/* Filter by area (package). "All" shows every area as its own row. */}
            <div className="flex flex-wrap items-center gap-1.5">
              {['all', ...areas].map((a) => {
                const active = area === a
                const count =
                  a === 'all' ? items.length : items.filter((p) => p.package === a).length
                return (
                  <button
                    key={a}
                    type="button"
                    onClick={() => setArea(a)}
                    className={cn(
                      'rounded-full border px-2.5 py-1 font-mono text-[11px] transition-colors',
                      active
                        ? 'border-primary/40 bg-primary/10 text-primary'
                        : 'border-border bg-card text-muted-foreground hover:text-foreground',
                    )}
                  >
                    {a === 'all' ? 'All' : a} <span className="opacity-60">{count}</span>
                  </button>
                )
              })}
            </div>

            {[...byArea.entries()].map(([pkg, prompts]) => (
              <section key={pkg} className="space-y-2">
                <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                  {pkg} <span className="text-muted-foreground/50">· {prompts.length}</span>
                </h3>
                <div className="flex gap-3 overflow-x-auto pb-2">
                  {prompts.map((p, i) => (
                    <motion.button
                      key={p.name}
                      type="button"
                      onClick={() => setSelected({ package: p.package, name: p.name })}
                      initial={{ opacity: 0, y: 10 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{
                        delay: Math.min(i * 0.02, 0.2),
                        type: 'spring',
                        stiffness: 320,
                        damping: 26,
                      }}
                      whileHover={{ y: -4 }}
                      whileTap={{ scale: 0.98 }}
                      className="group flex w-[240px] shrink-0 flex-col gap-2 rounded-xl border border-border bg-card p-4 text-left shadow-sm transition-colors hover:border-primary/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <FileText className="size-4 text-muted-foreground" />
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
                      </div>
                      <div className="truncate font-mono text-xs font-semibold text-foreground">
                        {p.name}
                      </div>
                      <p className="line-clamp-3 text-[11px] leading-relaxed text-muted-foreground">
                        {p.description}
                      </p>
                      <div className="mt-auto flex items-center gap-1 pt-1 text-[10px] font-medium text-primary opacity-0 transition-opacity group-hover:opacity-100">
                        <Pencil className="size-3" /> Click to edit
                      </div>
                    </motion.button>
                  ))}
                </div>
              </section>
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

  const approvals = (approvalsQ.data?.approvals ?? []).filter((a) => a.agent_did === did)
  const gated = (gatedQ.data?.gated ?? []).filter((g) => g.agent_id === agentId)

  return (
    <div className="space-y-6">
      <Section title={`Pending approvals (${approvals.length})`}>
        {approvals.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing is waiting on you for this agent.</p>
        ) : (
          <div className="space-y-2">
            {approvals.map((a) => (
              <ApprovalRequest key={a.id} a={a} operatorMode={operatorMode} />
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

const KNOWLEDGE_TABS = [
  { value: 'overview', label: 'Overview' },
  { value: 'insights', label: 'Insights' },
  { value: 'procedures', label: 'Procedures' },
  { value: 'entities', label: 'Entities' },
  { value: 'events', label: 'Events' },
  { value: 'daily-notes', label: 'Daily Notes' },
  { value: 'memories', label: 'Raw stream' },
]

/** GAP-1: the full Knowledge surface, scoped to this agent (reuses the browsers). */
function KnowledgeTab({ agentId }: { agentId: string }) {
  const query = useKnowledge(agentId)
  const [selectedEntitySlug, setSelectedEntitySlug] = useState<string | null>(null)
  const [tab, setTab] = useState('overview')
  const focusEntity = (slug: string) => {
    setSelectedEntitySlug(slug)
    setTab('entities')
  }
  return (
    <Tabs value={tab} onValueChange={setTab} className="flex flex-1 flex-col">
      <TabsList className="mb-4 flex-wrap">
        {KNOWLEDGE_TABS.map((t) => (
          <TabsTrigger key={t.value} value={t.value}>
            {t.label}
          </TabsTrigger>
        ))}
      </TabsList>
      <TabsContent value="overview">
        <QueryState query={query} isEmpty={() => !query.data}>
          {(data) => <KnowledgeOverview data={data} agentId={agentId} onNavigate={setTab} />}
        </QueryState>
      </TabsContent>
      <TabsContent value="insights">
        <InsightBrowser agentId={agentId} />
      </TabsContent>
      <TabsContent value="procedures">
        <ProcedureBrowser agentId={agentId} />
      </TabsContent>
      <TabsContent value="entities">
        <EntityBrowser
          agentId={agentId}
          selectedSlug={selectedEntitySlug}
          onSelectSlug={setSelectedEntitySlug}
        />
      </TabsContent>
      <TabsContent value="events">
        <EventBrowser agentId={agentId} />
      </TabsContent>
      <TabsContent value="daily-notes">
        <DailyNotesBrowser agentId={agentId} />
      </TabsContent>
      <TabsContent value="memories">
        <MemoryBrowser agentId={agentId} onNavigateEntity={focusEntity} />
      </TabsContent>
    </Tabs>
  )
}

/** GAP-2: this agent's runs — the step timeline + spawn lineage at agent scope. */
function RunsTab({ agentId }: { agentId: string }) {
  const runsQ = useRuns()
  const roster = useRoster()
  const [active, setActive] = useState<RunSummary | null>(null)
  const did = (roster.data?.agents ?? []).find((a) => a.agent_id === agentId)?.did ?? ''
  const runs = (runsQ.data?.runs ?? []).filter((r) => r.actor_did === did || r.agent === agentId)
  const held = runs.filter((r) => (r.status || '').toLowerCase() === 'held').length
  const failed = runs.filter((r) => (r.status || '').toLowerCase() === 'failed').length
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <InsightStat label="Runs" value={runs.length} />
        <InsightStat label="Held" value={held} />
        <InsightStat label="Failed" value={failed} />
      </div>
      {runs.length === 0 ? (
        <EmptyState
          title="No runs for this agent yet"
          description="Each user question to final response appears here as the agent works."
        />
      ) : (
        <div className="overflow-hidden rounded-lg border border-border bg-card">
          {runs.map((r, i) => (
            <button
              key={r.run_id}
              type="button"
              onClick={() => setActive(r)}
              className={cn(
                'flex w-full items-center gap-3 px-4 py-2.5 text-left hover:bg-muted/40',
                i > 0 && 'border-t border-border',
              )}
            >
              <span className="font-mono text-xs text-primary">{shortId(r.run_id, 16)}</span>
              <StatusChip value={r.status} />
              <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                {relativeTime(r.started_at)}
              </span>
            </button>
          ))}
        </div>
      )}
      <RunDetailDrawer run={active} open={!!active} onOpenChange={(o) => !o && setActive(null)} />
      <Section title="Spawn lineage">
        <div className="rounded-lg border border-border bg-card p-4">
          <SpawnLineage root={did || null} />
        </div>
      </Section>
    </div>
  )
}

function inboxIdempotencyKey(operation: string): string {
  return `${operation}-${crypto.randomUUID()}`
}

/** ArcTeam mail only. Sessions and gateway conversations remain separate tabs. */
function InboxTab({ agentId }: { agentId: string }) {
  const queryClient = useQueryClient()
  const roster = useRoster()
  const channelsQ = useAgentChannels(agentId)
  const inboxQ = useAgentInbox(agentId)
  const tasksQ = useAgentTasks(agentId)
  const approvalsQ = useApprovals()
  const [operatorMode] = useOperatorMode()
  const [active, setActive] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [replyBody, setReplyBody] = useState('')
  const [handoffRecipient, setHandoffRecipient] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<string | null>(null)
  const threadQ = useAgentInboxThread(agentId, active)
  const searchQ = useAgentInboxSearch(agentId, search)

  const did = (roster.data?.agents ?? []).find((a) => a.agent_id === agentId)?.did ?? ''
  const approvals = (approvalsQ.data?.approvals ?? []).filter((a) => a.agent_did === did)
  const channels = channelsQ.data?.channels ?? []
  const inbox = inboxQ.data?.threads ?? []
  const tasks = (tasksQ.data?.tasks ?? []) as unknown as Dict[]
  const reviewTasks = tasks.filter((t) => String(t.status) === 'review')
  const activeThread = inbox.find((thread) => thread.thread_id === active) ?? null
  const recipients =
    activeThread?.participants.filter(
      (item) => item.role === 'agent' || item.participant_id === did,
    ) ?? []

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ['agent', agentId, 'inbox'] })
  }

  const runMutation = async (operation: string, action: () => Promise<void>) => {
    setPending(operation)
    setError(null)
    try {
      await action()
      await refresh()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : 'Inbox action failed')
    } finally {
      setPending(null)
    }
  }

  const markRead = (messageId: string) =>
    runMutation(`read:${messageId}`, async () => {
      await apiPost(`/api/agents/${agentId}/inbox/messages/${encodeURIComponent(messageId)}/read`, {})
    })

  const sendReply = () => {
    if (!active || !replyBody.trim()) return
    void runMutation('reply', async () => {
      await apiPost(
        `/api/agents/${agentId}/inbox/${encodeURIComponent(active)}/reply`,
        { body: replyBody.trim() },
        { 'Idempotency-Key': inboxIdempotencyKey('reply') },
      )
      setReplyBody('')
    })
  }

  const createHandoff = () => {
    if (!active || !handoffRecipient) return
    void runMutation('handoff', async () => {
      await apiPost(
        `/api/agents/${agentId}/inbox/${encodeURIComponent(active)}/handoffs`,
        { to: [handoffRecipient] },
        { 'Idempotency-Key': inboxIdempotencyKey('handoff') },
      )
    })
  }

  const resolveHandoff = (handoffId: string, status: 'accepted' | 'declined') =>
    runMutation(`handoff:${handoffId}`, async () => {
      await apiPost(
        `/api/agents/${agentId}/inbox/handoffs/${encodeURIComponent(handoffId)}/resolution`,
        { status },
      )
    })

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatCard label="Delivery channels" value={channels.length} />
        <StatCard label="Inbox threads" value={inbox.length} />
        <StatCard label="Pending approvals" value={approvals.length} />
        <StatCard label="Awaiting review" value={reviewTasks.length} />
      </div>

      {(approvals.length > 0 || reviewTasks.length > 0) && (
        <Section title="Needs attention">
          <div className="space-y-2">
            {approvals.map((a) => (
              <ApprovalRequest key={a.id} a={a} operatorMode={operatorMode} />
            ))}
            {reviewTasks.map((t) => (
              <div
                key={String(t.id)}
                className="flex items-center justify-between gap-3 rounded-lg border border-border bg-card px-3 py-2 text-sm"
              >
                <span className="min-w-0 truncate text-foreground">
                  {String(t.description ?? t.title ?? t.id)}
                </span>
                <StatusChip value="review" />
              </div>
            ))}
          </div>
        </Section>
      )}

      <Section title="Delivery channels">
        {channels.length === 0 ? (
          <EmptyState title="No delivery channels seen yet" description="Channels appear as the agent receives messages." />
        ) : (
          <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border">
            {channels.map((c) => (
              <li key={c.target} className="flex items-center justify-between gap-3 bg-card px-3 py-2 text-sm">
                <span className="truncate text-foreground">{c.label}</span>
                <span className="shrink-0 rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                  {c.target}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Inbox threads">
        <div className="mb-2 flex max-w-xl gap-2">
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search this agent's mail…"
            aria-label="Search agent mail"
          />
          {search && (
            <Button variant="ghost" size="sm" onClick={() => setSearch('')}>
              Clear
            </Button>
          )}
        </div>
        {search.trim() && (
          <div className="mb-3 rounded-lg border border-border bg-card p-3">
            <div className="mb-2 text-xs font-medium text-muted-foreground">Search results</div>
            {searchQ.isLoading ? (
              <div className="text-sm text-muted-foreground">Searching mail…</div>
            ) : (searchQ.data?.messages ?? []).length === 0 ? (
              <div className="text-sm text-muted-foreground">No mail matched this search.</div>
            ) : (
              <div className="space-y-2">
                {(searchQ.data?.messages ?? []).map((message) => (
                  <button
                    key={message.message_id}
                    type="button"
                    onClick={() => setActive(message.thread_id)}
                    className="block w-full rounded border border-border/60 p-2 text-left text-sm hover:bg-muted/40"
                  >
                    <span className="mr-2 text-xs text-muted-foreground">{message.sender.participant_id}</span>
                    {message.body}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        {inbox.length === 0 ? (
          <EmptyState
            title="No inbox messages"
            description="Direct messages and mentions to this agent land here."
          />
        ) : (
          <div className="space-y-2">
            <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <Mail className="size-3.5" /> Durable Postgres mailbox · newest first
            </div>
            <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border">
              {inbox.map((thread) => (
                <li key={thread.thread_id}>
                  <button
                    type="button"
                    onClick={() => setActive(thread.thread_id)}
                    className="flex w-full items-center gap-3 bg-card px-3 py-2.5 text-left hover:bg-muted/40"
                  >
                    <span className={cn('size-2 rounded-full', thread.unread_count ? 'bg-status-online' : 'bg-muted')} />
                    <span className="min-w-0 flex-1 truncate text-sm text-foreground">
                      {thread.subject || thread.participants.map((p) => p.display_name || p.participant_id).join(', ')}
                    </span>
                    <span className="text-xs text-muted-foreground">{thread.unread_count} unread</span>
                  </button>
                </li>
              ))}
            </ul>
            {active && (
              <div className="space-y-2 rounded-lg border border-border bg-card p-3">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">Thread</span>
                  <Button variant="ghost" size="sm" onClick={() => setActive(null)}>Close</Button>
                </div>
                {threadQ.isLoading ? (
                  <div className="text-sm text-muted-foreground">Loading mail…</div>
                ) : (
                  (threadQ.data?.messages ?? []).map((message) => (
                    <div key={message.message_id} className="rounded bg-muted/40 p-2 text-sm">
                      <div className="mb-1 flex items-center gap-2 text-xs text-muted-foreground">
                        <span>{message.sender.display_name || message.sender.participant_id}</span>
                        <span>{relativeTime(message.created_at)}</span>
                        {operatorMode && (
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={pending === `read:${message.message_id}`}
                            onClick={() => markRead(message.message_id)}
                          >
                            Mark agent read
                          </Button>
                        )}
                      </div>
                      <div className="whitespace-pre-wrap">{message.body}</div>
                      {(message.attachments ?? []).length > 0 && (
                        <div className="mt-2 font-mono text-xs text-muted-foreground">
                          {(message.attachments ?? []).join(', ')}
                        </div>
                      )}
                    </div>
                  ))
                )}
                {(threadQ.data?.handoffs ?? []).map((handoff) => {
                  const id = String(handoff.handoff_id ?? '')
                  const status = String(handoff.status ?? 'pending')
                  const resolvedBy = (handoff.resolved_by as Dict | null)?.participant_id
                  const resolvedActor =
                    typeof handoff.resolved_actor_did === 'string'
                      ? handoff.resolved_actor_did
                      : null
                  return (
                    <div key={id} className="rounded border border-border/60 p-2 text-sm">
                      <div className="flex flex-wrap items-center gap-2">
                        <span>Handoff {id ? shortId(id, 12) : '—'}</span>
                        <StatusChip value={status} />
                        {operatorMode && status === 'pending' && id && (
                          <>
                            <Button
                              variant="outline"
                              size="sm"
                              disabled={pending === `handoff:${id}`}
                              onClick={() => void resolveHandoff(id, 'accepted')}
                            >
                              Accept for agent
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              disabled={pending === `handoff:${id}`}
                              onClick={() => void resolveHandoff(id, 'declined')}
                            >
                              Decline
                            </Button>
                          </>
                        )}
                      </div>
                      {resolvedActor && (
                        <div className="mt-1 text-xs text-muted-foreground">
                          {String(resolvedActor)} recorded {status} for {String(resolvedBy ?? 'the recipient')}.
                        </div>
                      )}
                    </div>
                  )
                })}
                {operatorMode && (
                  <div className="space-y-2 border-t border-border pt-3">
                    <Textarea
                      value={replyBody}
                      onChange={(event) => setReplyBody(event.target.value)}
                      placeholder="Reply as the signed operator…"
                      aria-label="Reply to mail thread"
                    />
                    <div className="flex flex-wrap items-center gap-2">
                      <Button size="sm" disabled={!replyBody.trim() || pending === 'reply'} onClick={sendReply}>
                        Send reply
                      </Button>
                      {recipients.length > 0 && (
                        <>
                          <Select value={handoffRecipient} onValueChange={setHandoffRecipient}>
                            <SelectTrigger className="h-8 w-[210px] text-xs">
                              <SelectValue placeholder="Handoff recipient…" />
                            </SelectTrigger>
                            <SelectContent>
                              {recipients.map((recipient) => (
                                <SelectItem key={recipient.participant_id} value={recipient.participant_id}>
                                  {recipient.display_name || recipient.participant_id}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={!handoffRecipient || pending === 'handoff'}
                            onClick={createHandoff}
                          >
                            Create handoff
                          </Button>
                        </>
                      )}
                    </div>
                    <p className="text-xs text-muted-foreground">
                      Replies are signed by the operator. Handoffs can only target a participant in this mail thread.
                    </p>
                  </div>
                )}
                {error && <div role="alert" className="text-sm text-status-error">{error}</div>}
              </div>
            )}
          </div>
        )}
      </Section>
    </div>
  )
}

const TAB_RENDER: Record<TabId, (agentId: string) => ReactNode> = {
  overview: (id) => <OverviewTab agentId={id} />,
  identity: (id) => <IdentityTab agentId={id} />,
  inbox: (id) => <InboxTab agentId={id} />,
  sessions: (id) => <SessionsTab agentId={id} />,
  runs: (id) => <RunsTab agentId={id} />,
  llm: (id) => <LlmTab agentId={id} />,
  skills: (id) => <SkillsTab agentId={id} />,
  tools: (id) => <ToolsTab agentId={id} />,
  prompts: (id) => <PromptsTab agentId={id} />,
  knowledge: (id) => <KnowledgeTab agentId={id} />,
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
  inbox: 'Inbox',
  sessions: 'Sessions',
  runs: 'Runs',
  llm: 'LLM',
  skills: 'Skills',
  tools: 'Tools',
  prompts: 'Prompts',
  knowledge: 'Knowledge',
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
