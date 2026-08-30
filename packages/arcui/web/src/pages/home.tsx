import { useMemo, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  ShieldAlert,
  PackageCheck,
  AlertTriangle,
  Eye,
  MessageCircleQuestion,
  ArrowRight,
  CheckCircle2,
  Boxes,
  Activity as ActivityIcon,
  Wrench,
  Coins,
  DollarSign,
  Zap,
} from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { InsightStat, StatusChip } from '@/components/ai'
import { StatusDot } from '@/components/status-badge'
import { AreaSeries, ChartCard } from '@/components/charts'
import { Sparkline } from '@/components/llm/sparkline'
import { AgentIdentity } from '@/components/AgentIdentity'
import {
  useHomeNeeds,
  useRuns,
  useTeamTasks,
  useRoster,
  useLlmStats,
  useTimeseries,
} from '@/lib/queries'
import type { HomeNeedsResponse } from '@/lib/queries'
import {
  initials,
  jobLabel,
  relativeTime,
  shortId,
  fmtTokens,
  fmtCost,
  fmtNumber,
} from '@/lib/format'
import type { Agent, AgentIdentityShape, RunSummary, TaskStatus } from '@/lib/types'


/** Momentum within the window: later-half sum vs earlier-half, as a percentage.
 * Undefined when there isn't enough signal to be honest about a direction. */
function windowTrend(values: number[]): number | undefined {
  if (values.length < 4) return undefined
  const mid = Math.floor(values.length / 2)
  const first = values.slice(0, mid).reduce((a, b) => a + b, 0)
  const second = values.slice(mid).reduce((a, b) => a + b, 0)
  if (first <= 0) return undefined
  return Math.round(((second - first) / first) * 100)
}

/** Even relative labels for the 24h token-volume chart (mirrors arcllm). */
function bucketLabels(n: number): string[] {
  if (n <= 1) return ['now']
  return Array.from({ length: n }, (_, i) =>
    i === n - 1 ? 'now' : `-${Math.round(((n - 1 - i) / (n - 1)) * 24)}h`,
  )
}

// Task-status breakdown segments. Static class strings — Tailwind JIT cannot
// see dynamically built names — mirroring the ai.tsx tone-class pattern.
const TASK_SEGMENTS: Array<{
  label: string
  bar: string
  dot: string
  match: (s: TaskStatus) => boolean
}> = [
  { label: 'Backlog', bar: 'bg-muted-foreground/40', dot: 'bg-muted-foreground/60', match: (s) => s === 'backlog' || s === 'todo' },
  { label: 'In progress', bar: 'bg-status-info', dot: 'bg-status-info', match: (s) => s === 'in_progress' },
  { label: 'Review', bar: 'bg-status-warning', dot: 'bg-status-warning', match: (s) => s === 'review' },
  { label: 'Done', bar: 'bg-status-online', dot: 'bg-status-online', match: (s) => s === 'done' },
  { label: 'Failed', bar: 'bg-status-error', dot: 'bg-status-error', match: (s) => s === 'failed' },
]

/**
 * Home / Today — the operator's daily driver. Answers "what needs me now?" and
 * "how is the fleet doing?" before any detail screen. Every tile deep-links to
 * its canonical screen, where the full actions live (progressive disclosure).
 */
export function HomePage() {
  const homeNeedsQ = useHomeNeeds()
  const runsQ = useRuns()
  // H-004: the Tasks/Runs summary cards below are "today" numbers, like every
  // other tile on this page — a separate 24h-windowed fetch feeds them so the
  // unwindowed lists above (failedRuns / reviewTasks in "Needs you", and
  // "Recent activity") keep surfacing older items exactly as before.
  const runsWindowedQ = useRuns('24h')
  const tasksWindowedQ = useTeamTasks('24h')
  const rosterQ = useRoster()
  const statsQ = useLlmStats('24h')
  const tsQ = useTimeseries('24h')

  // Each queue carries its OWN true count separately from its (possibly
  // shorter) preview list — "Needs you" must count everything waiting, not
  // just however many rows fit on this page (H-001).
  const approvalsQueue: HomeNeedsResponse['approvals'] = homeNeedsQ.data?.approvals ?? {
    count: 0,
    items: [],
  }
  const capabilitiesQueue: HomeNeedsResponse['capabilities'] = homeNeedsQ.data?.capabilities ?? {
    count: 0,
    items: [],
  }
  const reviewQueue: HomeNeedsResponse['review_tasks'] = homeNeedsQ.data?.review_tasks ?? {
    count: 0,
    items: [],
  }
  const waitingQueue: HomeNeedsResponse['waiting_on_human'] = homeNeedsQ.data?.waiting_on_human ?? {
    count: 0,
    items: [],
  }

  const runs = useMemo<RunSummary[]>(() => runsQ.data?.runs ?? [], [runsQ.data])
  const runsWindowed = useMemo<RunSummary[]>(
    () => runsWindowedQ.data?.runs ?? [],
    [runsWindowedQ.data],
  )
  const tasksWindowed = useMemo(() => tasksWindowedQ.data?.tasks ?? [], [tasksWindowedQ.data])
  const agents = useMemo<Agent[]>(
    () => (rosterQ.data?.agents ?? []).filter((a) => !a.hidden),
    [rosterQ.data],
  )

  const nameByDid = useMemo(() => {
    const m = new Map<string, string>()
    for (const a of agents) {
      if (a.did) m.set(a.did, a.display_name || a.name || a.agent_id || a.did)
    }
    return m
  }, [agents])
  // Full agent rows keyed both ways — H-007's AgentIdentity needs the whole
  // resolved shape (name + type/host + DID), not just a display string.
  const agentByDid = useMemo(() => {
    const m = new Map<string, Agent>()
    for (const a of agents) if (a.did) m.set(a.did, a)
    return m
  }, [agents])
  const agentByAgentId = useMemo(() => {
    const m = new Map<string, Agent>()
    for (const a of agents) if (a.agent_id) m.set(a.agent_id, a)
    return m
  }, [agents])
  const runAgent = (r: RunSummary) =>
    (r.actor_did && nameByDid.get(r.actor_did)) || nameByDid.get(r.agent) || r.agent

  const failedRunsAll = runs.filter((r) => (r.status || '').toLowerCase() === 'failed')
  const failedRuns = failedRunsAll.slice(0, 5)
  const online = agents.filter((a) => a.online).length
  const running = runs.filter((r) =>
    ['running', 'in_progress'].includes((r.status || '').toLowerCase()),
  ).length
  const needsYou =
    approvalsQueue.count +
    capabilitiesQueue.count +
    reviewQueue.count +
    waitingQueue.count +
    failedRunsAll.length
  const recent = runs.slice(0, 6)

  // --- Activity (last 24h) — authoritative LLM stats + the run snapshot ------
  const stats = statsQ.data
  const buckets = tsQ.data?.buckets ?? []
  const labels = bucketLabels(buckets.length)
  const volume = buckets.map((b, i) => ({ label: labels[i], tokens: b.total_tokens }))
  const requestSpark = buckets.map((b) => b.request_count)
  const tokenSpark = buckets.map((b) => b.total_tokens)
  const toolCalls = runs.reduce((sum, r) => sum + (r.tool_calls ?? 0), 0)

  // Run outcomes over the last 24h (H-004) — "how did today's work go".
  // `running` stays un-windowed: a run still in flight counts as running
  // regardless of when it started.
  const runStatus = useMemo(() => {
    const c = { running, completed: 0, failed: 0, stale: 0 }
    for (const r of runsWindowed) {
      const s = (r.status || '').toLowerCase()
      if (['completed', 'success', 'done', 'ok'].includes(s)) c.completed += 1
      else if (['failed', 'error'].includes(s)) c.failed += 1
      else if (s === 'stale') c.stale += 1
    }
    return c
  }, [runsWindowed, running])

  // --- State (totals) — tasks by status, last 24h (H-004) -------------------
  const taskCounts = useMemo(() => {
    const total = tasksWindowed.length
    const segs = TASK_SEGMENTS.map((seg) => ({
      ...seg,
      count: tasksWindowed.filter((t) => seg.match((t.status ?? 'backlog') as TaskStatus)).length,
    }))
    return { total, segs }
  }, [tasksWindowed])

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Home"
        description="What needs you right now, and how your agents are doing."
      />
      <div className="flex-1 space-y-6 overflow-auto p-6">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <InsightStat label="Agents" value={agents.length} icon={<Boxes className="size-4" />} />
          <InsightStat label="Online" value={online} />
          <InsightStat label="Needs you" value={needsYou} />
          <InsightStat label="Running" value={running} icon={<ActivityIcon className="size-4" />} />
        </div>

        {/* Activity — last 24h */}
        <section className="space-y-2.5">
          <SectionHead title="Activity · last 24h" href="/arcllm" cta="LLM metrics" />
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <InsightStat
              label="LLM calls"
              value={fmtNumber(stats?.request_count ?? 0)}
              delta={windowTrend(requestSpark)}
              spark={<Sparkline data={requestSpark} />}
              icon={<Zap className="size-4" />}
            />
            <InsightStat
              label="Tokens"
              value={fmtTokens(stats?.total_tokens ?? 0)}
              delta={windowTrend(tokenSpark)}
              spark={<Sparkline data={tokenSpark} color="var(--chart-1)" />}
              icon={<Coins className="size-4" />}
            />
            <InsightStat
              label="Cost"
              value={fmtCost(stats?.total_cost ?? 0)}
              icon={<DollarSign className="size-4" />}
            />
            <InsightStat
              label="Tool calls · recent"
              value={fmtNumber(toolCalls)}
              icon={<Wrench className="size-4" />}
            />
          </div>
        </section>

        {/* Work state — token volume + task/run breakdowns */}
        <div className="grid gap-6 lg:grid-cols-2">
          <ChartCard title="Token volume · 24h">
            {volume.length > 1 ? (
              <AreaSeries data={volume} dataKey="tokens" />
            ) : (
              <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                Not enough activity yet to chart.
              </div>
            )}
          </ChartCard>

          <div className="space-y-4 rounded-lg border border-border bg-card p-4">
            <div>
              <div className="mb-2 flex items-center justify-between">
                <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                  Tasks
                </h3>
                <span className="text-[11px] font-semibold tabular-nums text-muted-foreground">
                  {taskCounts.total} total
                </span>
              </div>
              <StackedBar segs={taskCounts.segs} total={taskCounts.total} />
              <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3">
                {taskCounts.segs.map((s) => (
                  <div key={s.label} className="flex items-center gap-2 text-xs">
                    <span className={`size-2 shrink-0 rounded-full ${s.dot}`} />
                    <span className="min-w-0 flex-1 truncate text-muted-foreground">{s.label}</span>
                    <span className="font-semibold tabular-nums text-foreground">{s.count}</span>
                  </div>
                ))}
              </div>
            </div>

            <div className="border-t border-border pt-3">
              <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                Runs
              </h3>
              <div className="grid grid-cols-4 gap-2 text-center">
                <RunStat label="Running" value={runStatus.running} tone="text-status-info" />
                <RunStat label="Done" value={runStatus.completed} tone="text-status-online" />
                <RunStat label="Failed" value={runStatus.failed} tone="text-status-error" />
                <RunStat label="Stale" value={runStatus.stale} tone="text-status-warning" />
              </div>
            </div>
          </div>
        </div>

        {/* Needs you */}
        <section className="space-y-2.5">
          <SectionHead title="Needs you" href="/approvals" cta="All approvals" />
          {needsYou === 0 ? (
            <div className="flex items-center gap-2.5 rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">
              <CheckCircle2 className="size-4 text-status-online" />
              You&rsquo;re all caught up. Nothing is waiting on you.
            </div>
          ) : (
            <div className="grid gap-2.5">
              {approvalsQueue.items.map((a) => (
                <NeedsRow
                  key={`ap-${a.id}`}
                  tone="warning"
                  icon={<ShieldAlert className="size-4" />}
                  identity={agentByDid.get(a.agent_did)?.identity}
                  fallbackName={a.agent_label}
                  color={agentByDid.get(a.agent_did)?.color}
                  message={
                    <>
                      needs your approval to run <span className="font-mono">{a.tool}</span>
                      {a.legs?.length ? ` · ${a.legs.join(', ')}` : ''}
                    </>
                  }
                  href="/approvals"
                  cta="Review"
                />
              ))}
              {capabilitiesQueue.items.map((c) => (
                <NeedsRow
                  key={`cap-${c.agent_id}-${c.name}`}
                  tone="warning"
                  icon={<PackageCheck className="size-4" />}
                  identity={agentByAgentId.get(c.agent_id)?.identity}
                  fallbackName={c.agent_label}
                  color={agentByAgentId.get(c.agent_id)?.color}
                  message={
                    <>
                      needs your review before its {c.kind}{' '}
                      <span className="font-mono">{c.name}</span> can load
                    </>
                  }
                  href="/gated"
                  cta="Review"
                />
              ))}
              {failedRuns.map((r) => (
                <NeedsRow
                  key={`run-${r.run_id}`}
                  tone="error"
                  icon={<AlertTriangle className="size-4" />}
                  identity={
                    (r.actor_did && agentByDid.get(r.actor_did)?.identity) ||
                    agentByDid.get(r.agent)?.identity
                  }
                  fallbackName={runAgent(r)}
                  color={
                    (r.actor_did && agentByDid.get(r.actor_did)?.color) ||
                    agentByDid.get(r.agent)?.color
                  }
                  message={
                    <>
                      run failed · <span className="font-mono">{shortId(r.run_id, 12)}</span> ·{' '}
                      {relativeTime(r.started_at)}
                    </>
                  }
                  href={`/arcrun?run=${encodeURIComponent(r.run_id)}`}
                  cta="Open"
                />
              ))}
              {reviewQueue.items.map((t) => (
                <NeedsRow
                  key={`task-${t.id}`}
                  tone="info"
                  icon={<Eye className="size-4" />}
                  identity={t.owner_did ? agentByDid.get(t.owner_did)?.identity : undefined}
                  fallbackName={t.owner_did ? undefined : 'A task'}
                  color={t.owner_did ? agentByDid.get(t.owner_did)?.color : undefined}
                  message={
                    <>
                      is waiting for your review ·{' '}
                      <span className="font-mono">{shortId(String(t.id ?? ''), 12)}</span>
                    </>
                  }
                  href="/tasks"
                  cta="Review"
                />
              ))}
              {waitingQueue.items.map((w) => (
                <NeedsRow
                  key={`wait-${w.message_id}`}
                  tone="info"
                  icon={<MessageCircleQuestion className="size-4" />}
                  identity={agentByDid.get(w.agent_did)?.identity}
                  fallbackName={nameByDid.get(w.agent_did) ?? 'An agent'}
                  color={agentByDid.get(w.agent_did)?.color}
                  message={
                    <>
                      is waiting on your reply in{' '}
                      <span className="font-mono">#{w.channel}</span>
                      {w.preview ? ` · ${w.preview}` : ''}
                    </>
                  }
                  href="/messages"
                  cta="Reply"
                />
              ))}
            </div>
          )}
        </section>

        <div className="grid gap-6 lg:grid-cols-2">
          {/* Fleet at a glance */}
          <section className="space-y-2.5">
            <SectionHead title="Fleet" href="/agents" cta="All agents" />
            <div className="rounded-lg border border-border bg-card">
              {agents.length === 0 ? (
                <p className="p-4 text-sm text-muted-foreground">No agents registered yet.</p>
              ) : (
                agents.slice(0, 8).map((a, i) => {
                  const label = a.display_name || a.name || a.agent_id || 'unknown'
                  return (
                    <Link
                      key={a.agent_id}
                      to={`/agents/${a.agent_id}`}
                      className={`flex items-center gap-3 px-4 py-2.5 hover:bg-muted/40 ${i > 0 ? 'border-t border-border' : ''}`}
                    >
                      <span
                        className="grid size-7 shrink-0 place-items-center rounded-md text-[11px] font-semibold text-primary-foreground"
                        style={{ background: a.color || 'var(--primary)' }}
                      >
                        {initials(label)}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">
                        {label}
                      </span>
                      <StatusDot online={a.online} />
                    </Link>
                  )
                })
              )}
            </div>
          </section>

          {/* Recent activity */}
          <section className="space-y-2.5">
            <SectionHead title="Recent activity" href="/arcrun" cta="All activity" />
            <div className="rounded-lg border border-border bg-card">
              {recent.length === 0 ? (
                <p className="p-4 text-sm text-muted-foreground">No runs yet.</p>
              ) : (
                recent.map((r, i) => (
                  <Link
                    key={r.run_id}
                    to={`/arcrun?run=${encodeURIComponent(r.run_id)}`}
                    className={`flex items-center gap-3 px-4 py-2.5 hover:bg-muted/40 ${i > 0 ? 'border-t border-border' : ''}`}
                  >
                    <div className="flex min-w-0 flex-1 flex-col">
                      <span className="truncate text-sm font-medium text-foreground">
                        {runAgent(r)}
                      </span>
                      {jobLabel(r.job) && (
                        <span
                          className="truncate text-[11px] leading-tight text-foreground/55"
                          title="A background job the agent ran on its own (not a person-driven run)"
                        >
                          {jobLabel(r.job)}
                        </span>
                      )}
                    </div>
                    <StatusChip value={r.status} />
                    <span className="whitespace-nowrap text-xs text-muted-foreground">
                      {relativeTime(r.started_at)}
                    </span>
                  </Link>
                ))
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}

function SectionHead({ title, href, cta }: { title: string; href: string; cta: string }) {
  return (
    <div className="flex items-center justify-between">
      <h2 className="font-display text-sm font-bold uppercase tracking-[0.1em] text-muted-foreground">
        {title}
      </h2>
      <Link
        to={href}
        className="inline-flex items-center gap-1 text-xs font-semibold text-primary hover:underline"
      >
        {cta} <ArrowRight className="size-3" />
      </Link>
    </div>
  )
}

/** A single proportional bar of task-status segments. Empty → a flat track. */
function StackedBar({
  segs,
  total,
}: {
  segs: Array<{ label: string; bar: string; count: number }>
  total: number
}) {
  if (total === 0) {
    return <div className="h-2.5 rounded-full bg-muted" />
  }
  return (
    <div className="flex h-2.5 overflow-hidden rounded-full bg-muted">
      {segs
        .filter((s) => s.count > 0)
        .map((s) => (
          <div
            key={s.label}
            className={s.bar}
            style={{ width: `${(s.count / total) * 100}%` }}
            title={`${s.label}: ${s.count}`}
          />
        ))}
    </div>
  )
}

/** One run-outcome count, big number over a muted label. */
function RunStat({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className="rounded-md bg-muted/40 py-2">
      <div className={`font-display text-lg font-extrabold leading-none tabular-nums ${tone}`}>
        {value}
      </div>
      <div className="mt-1 text-[10px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
        {label}
      </div>
    </div>
  )
}

const TONE: Record<string, string> = {
  warning: 'text-status-warning',
  error: 'text-status-error',
  info: 'text-status-info',
}

/** One "Needs you" row. When the item names an agent (`identity` resolved
 * off the roster by DID), the row leads with the H-007 `AgentIdentity` block
 * — the ONE renderer for an agent's name + type/host + DID across the
 * dashboard — instead of hand-rolling a name string; `fallbackName` alone
 * covers items with no agent to show (or an agent not in the roster). */
function NeedsRow({
  tone,
  icon,
  identity,
  fallbackName,
  color,
  message,
  href,
  cta,
}: {
  tone: 'warning' | 'error' | 'info'
  icon: ReactNode
  identity?: AgentIdentityShape
  fallbackName?: string
  color?: string
  message: ReactNode
  href: string
  cta: string
}) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-border bg-card p-3.5">
      <span className={`grid size-8 shrink-0 place-items-center rounded-lg bg-muted/60 ${TONE[tone]}`}>
        {icon}
      </span>
      <div className="min-w-0 flex-1">
        {identity ? (
          <AgentIdentity
            identity={identity}
            fallbackName={fallbackName}
            color={color}
            size="sm"
            showAvatar={false}
            className="mb-1"
          />
        ) : (
          fallbackName && (
            <div className="truncate text-sm font-semibold text-foreground">{fallbackName}</div>
          )
        )}
        <div className="truncate text-xs text-muted-foreground">{message}</div>
      </div>
      <Link
        to={href}
        className="shrink-0 rounded-md border border-border bg-secondary px-3 py-1.5 text-xs font-semibold text-secondary-foreground hover:border-foreground/20"
      >
        {cta}
      </Link>
    </div>
  )
}
