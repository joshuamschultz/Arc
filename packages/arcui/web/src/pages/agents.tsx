import { useNavigate } from 'react-router-dom'
import { Boxes, Cpu } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { StatCard } from '@/components/stat-card'
import { StatusDot } from '@/components/status-badge'
import { QueryState, EmptyState } from '@/components/states'
import {
  useAgentPolicyStats,
  useAgentSchedules,
  useAgentSessions,
  useAgentTimeseries,
  useRoster,
} from '@/lib/queries'
import { initials } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { Agent } from '@/lib/types'

/** Tiny inline activity sparkline (24h token volume per bucket). */
function Sparkline({ values }: { values: number[] }) {
  const max = Math.max(1, ...values)
  return (
    <div className="flex h-7 items-end gap-0.5">
      {values.length === 0 ? (
        <span className="text-[11px] text-muted-foreground">No 24h activity</span>
      ) : (
        values.map((v, i) => (
          <div
            key={i}
            className="w-full min-w-0.5 flex-1 rounded-sm bg-primary/60"
            style={{ height: `${Math.max(6, Math.round((v / max) * 100))}%` }}
          />
        ))
      )}
    </div>
  )
}

function Metric({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="text-center">
      <div className="text-sm font-semibold tabular-nums text-foreground">{value}</div>
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
    </div>
  )
}

function AgentCardMetrics({ agentId }: { agentId: string }) {
  const sessions = useAgentSessions(agentId)
  const schedules = useAgentSchedules(agentId)
  const policy = useAgentPolicyStats(agentId)
  const ts = useAgentTimeseries(agentId, '24h')
  const volume = (ts.data?.buckets ?? []).map((b) => b.total_tokens ?? 0)
  return (
    <>
      <div className="grid grid-cols-3 gap-2 border-t border-border/60 pt-2">
        <Metric label="Sessions" value={sessions.data?.sessions?.length ?? 0} />
        <Metric label="Schedules" value={schedules.data?.schedules?.length ?? 0} />
        <Metric label="Bullets" value={policy.data?.total ?? 0} />
      </div>
      <Sparkline values={volume} />
    </>
  )
}

function AgentCard({ agent, onOpen }: { agent: Agent; onOpen: () => void }) {
  const label = agent.display_name || agent.name || agent.agent_id || 'unknown'
  return (
    <button
      type="button"
      onClick={onOpen}
      className={cn(
        'flex cursor-pointer flex-col gap-3 rounded-xl border border-border bg-card p-4 text-left',
        'shadow-xs transition-colors hover:border-primary/40 hover:bg-muted/30',
      )}
    >
      <div className="flex items-center gap-3">
        <span
          className="flex size-10 shrink-0 items-center justify-center rounded-lg text-sm font-semibold text-primary-foreground"
          style={{ background: agent.color || 'var(--primary)' }}
        >
          {initials(label)}
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate font-semibold text-foreground">{label}</div>
          {agent.role_label && (
            <div className="truncate text-xs text-muted-foreground">{agent.role_label}</div>
          )}
        </div>
        <StatusDot online={agent.online} />
      </div>

      {agent.did && (
        <div className="truncate font-mono text-xs text-muted-foreground">{agent.did}</div>
      )}

      <div className="flex flex-wrap gap-1.5">
        {agent.model && (
          <span className="inline-flex items-center gap-1 rounded-md border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] text-muted-foreground">
            <Cpu className="size-3" />
            {agent.model}
          </span>
        )}
        {agent.provider && (
          <span className="rounded-md border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] text-muted-foreground">
            {agent.provider}
          </span>
        )}
        {agent.type && (
          <span className="rounded-md border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] text-muted-foreground">
            {agent.type}
          </span>
        )}
      </div>

      {agent.agent_id && <AgentCardMetrics agentId={agent.agent_id} />}
    </button>
  )
}

export function AgentsPage() {
  const navigate = useNavigate()
  const query = useRoster()
  const agents = (query.data?.agents ?? []).filter((a) => !a.hidden)

  const online = agents.filter((a) => a.online).length
  const idle = agents.length - online
  const models = new Set(agents.map((a) => a.model).filter(Boolean)).size

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Agents" description="The agent fleet and status." />
      <div className="flex-1 space-y-5 overflow-auto p-6">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard label="Agents" value={agents.length} icon={<Boxes className="size-4" />} />
          <StatCard label="Online" value={online} />
          <StatCard label="Idle" value={idle} />
          <StatCard label="Models" value={models} />
        </div>

        <QueryState
          query={query}
          isEmpty={() => agents.length === 0}
          empty={
            <EmptyState
              icon={<Boxes className="size-7" />}
              title="No agents registered"
              description="Register an agent with `arc team register` and restart the UI to see it here."
            />
          }
        >
          {() => (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {agents.map((agent) => (
                <AgentCard
                  key={agent.agent_id}
                  agent={agent}
                  onOpen={() => navigate(`/agents/${agent.agent_id}`)}
                />
              ))}
            </div>
          )}
        </QueryState>
      </div>
    </div>
  )
}
