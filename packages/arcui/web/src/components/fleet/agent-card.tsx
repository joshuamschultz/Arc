import type { ReactNode } from 'react'
import { Cpu, Server, Boxes } from 'lucide-react'
import { StatusDot } from '@/components/status-badge'
import {
  useAgentPolicyStats,
  useAgentSchedules,
  useAgentSessions,
  useAgentTimeseries,
} from '@/lib/queries'
import { initials } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { Agent } from '@/lib/types'

/**
 * 24h activity as solid bars (no gradient). Past buckets read as a quiet
 * primary wash; the most recent bucket is emphasized at full strength so the
 * eye lands on "now" (REDESIGN §12.4 — emphasized endpoint).
 */
function Sparkline({ values }: { values: number[] }) {
  if (values.length === 0) {
    return (
      <div className="flex h-8 items-center text-[11px] text-muted-foreground">
        No activity in the last 24h
      </div>
    )
  }
  const max = Math.max(1, ...values)
  const last = values.length - 1
  return (
    <div className="flex h-8 items-end gap-px" aria-hidden>
      {values.map((v, i) => (
        <div
          key={i}
          className={cn(
            'w-full min-w-0.5 flex-1 rounded-[1px]',
            i === last ? 'bg-primary' : 'bg-primary/35',
          )}
          style={{ height: `${Math.max(6, Math.round((v / max) * 100))}%` }}
        />
      ))}
    </div>
  )
}

function Metric({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="flex flex-col items-center gap-0.5">
      <div className="font-display text-base font-bold tabular-nums leading-none text-foreground">
        {value}
      </div>
      <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
        {label}
      </div>
    </div>
  )
}

/** Metrics footer: work volume + the three per-agent counts. */
function AgentCardMetrics({ agentId }: { agentId: string }) {
  const sessions = useAgentSessions(agentId)
  const schedules = useAgentSchedules(agentId)
  const policy = useAgentPolicyStats(agentId)
  const ts = useAgentTimeseries(agentId, '24h')
  const volume = (ts.data?.buckets ?? []).map((b) => b.total_tokens ?? 0)
  return (
    <div className="mt-auto flex flex-col gap-3">
      <div>
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground/70">
          24h activity
        </div>
        <Sparkline values={volume} />
      </div>
      <div className="grid grid-cols-3 gap-2 border-t border-border/60 pt-3">
        <Metric label="Sessions" value={sessions.data?.sessions?.length ?? 0} />
        <Metric label="Schedules" value={schedules.data?.schedules?.length ?? 0} />
        <Metric label="Bullets" value={policy.data?.total ?? 0} />
      </div>
    </div>
  )
}

/** A neutral, hairline capability chip (model / provider / type). */
function Chip({ icon, children }: { icon?: ReactNode; children: ReactNode }) {
  return (
    <span className="inline-flex max-w-full items-center gap-1 rounded-md border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] text-muted-foreground">
      {icon}
      <span className="truncate">{children}</span>
    </span>
  )
}

/**
 * A premium agent roster card. The avatar's brand color is the only color pop;
 * everything else stays flat and neutral so the fleet reads as one calm grid.
 * The whole card is the click target → the agent's detail route.
 */
export function AgentCard({ agent, onOpen }: { agent: Agent; onOpen: () => void }) {
  const label = agent.display_name || agent.name || agent.agent_id || 'unknown'
  return (
    <button
      type="button"
      onClick={onOpen}
      className={cn(
        'group flex h-full min-h-[15rem] w-full cursor-pointer flex-col gap-3 rounded-xl border border-border bg-card p-4 text-left',
        'shadow-xs transition-all duration-200 hover:-translate-y-0.5 hover:border-primary/40 hover:shadow-sm',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60',
      )}
    >
      <div className="flex items-start gap-3">
        <span
          className="flex size-11 shrink-0 items-center justify-center rounded-lg text-sm font-bold text-primary-foreground ring-1 ring-inset ring-black/10"
          style={{ background: agent.color || 'var(--primary)' }}
        >
          {initials(label)}
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate font-display text-[15px] font-bold tracking-tight text-foreground">
            {label}
          </div>
          <div className="truncate text-xs text-muted-foreground">
            {agent.role_label || 'Agent'}
          </div>
        </div>
        <StatusDot online={agent.online} className="shrink-0" />
      </div>

      {agent.did && (
        <div className="truncate rounded-md border border-border bg-muted/40 px-1.5 py-1 font-mono text-[11px] text-muted-foreground">
          {agent.did}
        </div>
      )}

      <div className="flex flex-wrap gap-1.5">
        {agent.model && <Chip icon={<Cpu className="size-3 shrink-0" />}>{agent.model}</Chip>}
        {agent.provider && (
          <Chip icon={<Server className="size-3 shrink-0" />}>{agent.provider}</Chip>
        )}
        {agent.type && <Chip icon={<Boxes className="size-3 shrink-0" />}>{agent.type}</Chip>}
      </div>

      {agent.agent_id && <AgentCardMetrics agentId={agent.agent_id} />}
    </button>
  )
}
