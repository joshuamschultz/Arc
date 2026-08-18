import { SignedSeal } from '@/components/hitl'
import { useAgentTimeseries } from '@/lib/queries'
import { initials } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { Agent } from '@/lib/types'

/** Optional live fields a running fleet reports (populated by the backend). */
type FleetAgent = Agent & {
  activity?: string
  current_action?: string
  signed_today?: number
}

/**
 * 24h activity as solid bars (no gradient). The most recent bucket is
 * emphasized at full strength so the eye lands on "now".
 */
function Sparkline({ values }: { values: number[] }) {
  if (values.length === 0) return <div className="h-7" />
  const max = Math.max(1, ...values)
  const last = values.length - 1
  return (
    <div className="flex h-7 items-end gap-px" aria-hidden>
      {values.map((v, i) => (
        <div
          key={i}
          className={cn('w-full min-w-0.5 flex-1 rounded-[1px]', i === last ? 'bg-primary' : 'bg-primary/35')}
          style={{ height: `${Math.max(8, Math.round((v / max) * 100))}%` }}
        />
      ))}
    </div>
  )
}

function CardSpark({ agentId }: { agentId: string }) {
  const ts = useAgentTimeseries(agentId, '24h')
  return <Sparkline values={(ts.data?.buckets ?? []).map((b) => b.total_tokens ?? 0)} />
}

const ACTIVITY_DOT: Record<string, string> = {
  Building: 'bg-status-online',
  Online: 'bg-status-online',
  Waiting: 'bg-status-warning',
  Error: 'bg-status-error',
  Review: 'bg-status-info',
  Scheduled: 'bg-status-idle',
  Idle: 'bg-status-idle',
}

function ActivityBadge({ activity, online }: { activity?: string; online?: boolean }) {
  const label = activity ?? (online ? 'Online' : 'Idle')
  return (
    <span className="inline-flex shrink-0 items-center gap-1.5 text-xs font-medium text-muted-foreground">
      <span className={cn('size-2 rounded-full', ACTIVITY_DOT[label] ?? 'bg-status-idle')} />
      {label}
    </span>
  )
}

/**
 * Agent card, mockup-faithful: who the agent is, what it's doing right now, and
 * a calm proof-of-work footer — how many actions it has signed today, with a
 * 24h activity sparkline. The whole card opens the agent's detail.
 */
export function AgentCard({ agent, onOpen }: { agent: Agent; onOpen: () => void }) {
  const a = agent as FleetAgent
  const label = a.display_name || a.name || a.agent_id || 'unknown'
  return (
    <button
      type="button"
      onClick={onOpen}
      className={cn(
        'group flex h-full w-full cursor-pointer flex-col gap-3 rounded-xl border border-border bg-card p-4 text-left',
        'shadow-xs transition-all duration-200 hover:-translate-y-0.5 hover:border-primary/40 hover:shadow-sm',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60',
      )}
    >
      <div className="flex items-center gap-3">
        <span
          className="flex size-10 shrink-0 items-center justify-center rounded-lg text-sm font-bold text-white ring-1 ring-inset ring-black/10"
          style={{ background: a.color || 'var(--primary)' }}
        >
          {initials(label)}
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate font-display text-[15px] font-bold tracking-tight text-foreground">
            {label}
          </div>
          <div className="truncate text-xs text-muted-foreground">{a.role_label || 'Agent'}</div>
        </div>
        <ActivityBadge activity={a.activity} online={a.online} />
      </div>

      <div className="min-h-5 text-sm text-muted-foreground">
        {a.current_action ?? (a.online ? 'Online' : 'Idle')}
      </div>

      <div className="mt-auto flex items-end justify-between gap-4 border-t border-border/60 pt-3">
        <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-[11px] font-medium text-signed">
          <SignedSeal className="size-3.5" />
          {a.signed_today ?? 0} signed today
        </span>
        {a.agent_id && (
          <div className="w-24">
            <CardSpark agentId={a.agent_id} />
          </div>
        )}
      </div>
    </button>
  )
}
