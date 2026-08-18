import { useMemo, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  ShieldAlert,
  AlertTriangle,
  Eye,
  ArrowRight,
  CheckCircle2,
  Boxes,
  Activity as ActivityIcon,
} from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { StatCard } from '@/components/stat-card'
import { StatusDot, StatusText } from '@/components/status-badge'
import { useApprovals, useRuns, useTeamTasks, useRoster } from '@/lib/queries'
import { initials, relativeTime, shortId } from '@/lib/format'
import type { Agent, RunSummary } from '@/lib/types'

/**
 * Home / Today — the operator's daily driver. Answers "what needs me now?" and
 * "how is the fleet doing?" before any detail screen. Every tile deep-links to
 * its canonical screen, where the full actions live (progressive disclosure).
 */
export function HomePage() {
  const approvalsQ = useApprovals()
  const runsQ = useRuns()
  const tasksQ = useTeamTasks()
  const rosterQ = useRoster()

  const approvals = approvalsQ.data?.approvals ?? []
  const runs = useMemo<RunSummary[]>(() => runsQ.data?.runs ?? [], [runsQ.data])
  const tasks = tasksQ.data?.tasks ?? []
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
  const runAgent = (r: RunSummary) =>
    (r.actor_did && nameByDid.get(r.actor_did)) || nameByDid.get(r.agent) || r.agent

  const failedRuns = runs
    .filter((r) => (r.status || '').toLowerCase() === 'failed')
    .slice(0, 5)
  const reviewTasks = tasks.filter((t) => t.status === 'review').slice(0, 5)
  const online = agents.filter((a) => a.online).length
  const running = runs.filter((r) =>
    ['running', 'in_progress'].includes((r.status || '').toLowerCase()),
  ).length
  const needsYou = approvals.length + failedRuns.length + reviewTasks.length
  const recent = runs.slice(0, 6)

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Home"
        description="What needs you right now, and how your agents are doing."
      />
      <div className="flex-1 space-y-6 overflow-auto p-6">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard label="Agents" value={agents.length} icon={<Boxes className="size-4" />} />
          <StatCard label="Online" value={online} />
          <StatCard label="Needs you" value={needsYou} />
          <StatCard label="Running" value={running} icon={<ActivityIcon className="size-4" />} />
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
              {approvals.map((a) => (
                <NeedsRow
                  key={`ap-${a.id}`}
                  tone="warning"
                  icon={<ShieldAlert className="size-4" />}
                  title={`${a.agent_label} needs your approval`}
                  sub={
                    <>
                      to run <span className="font-mono">{a.tool}</span>
                      {a.legs?.length ? ` · ${a.legs.join(', ')}` : ''}
                    </>
                  }
                  href="/approvals"
                  cta="Review"
                />
              ))}
              {failedRuns.map((r) => (
                <NeedsRow
                  key={`run-${r.run_id}`}
                  tone="error"
                  icon={<AlertTriangle className="size-4" />}
                  title={`${runAgent(r)}'s run failed`}
                  sub={
                    <>
                      run <span className="font-mono">{shortId(r.run_id, 12)}</span> ·{' '}
                      {relativeTime(r.started_at)}
                    </>
                  }
                  href={`/arcrun?run=${encodeURIComponent(r.run_id)}`}
                  cta="Open"
                />
              ))}
              {reviewTasks.map((t) => (
                <NeedsRow
                  key={`task-${t.id}`}
                  tone="info"
                  icon={<Eye className="size-4" />}
                  title="A task is waiting for your review"
                  sub={<span className="font-mono">{shortId(String(t.id ?? ''), 12)}</span>}
                  href="/tasks"
                  cta="Review"
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
                    <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">
                      {runAgent(r)}
                    </span>
                    <StatusText value={r.status} />
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

const TONE: Record<string, string> = {
  warning: 'text-status-warning',
  error: 'text-status-error',
  info: 'text-status-info',
}

function NeedsRow({
  tone,
  icon,
  title,
  sub,
  href,
  cta,
}: {
  tone: 'warning' | 'error' | 'info'
  icon: ReactNode
  title: string
  sub: ReactNode
  href: string
  cta: string
}) {
  return (
    <div className="flex items-center gap-3 rounded-lg border border-border bg-card p-3.5">
      <span className={`grid size-8 shrink-0 place-items-center rounded-lg bg-muted/60 ${TONE[tone]}`}>
        {icon}
      </span>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-semibold text-foreground">{title}</div>
        <div className="truncate text-xs text-muted-foreground">{sub}</div>
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
