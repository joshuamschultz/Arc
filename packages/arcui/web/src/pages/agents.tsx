import { useNavigate } from 'react-router-dom'
import { Boxes, Cpu } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { StatCard } from '@/components/stat-card'
import { StatusDot } from '@/components/status-badge'
import { QueryState, EmptyState } from '@/components/states'
import { useRoster } from '@/lib/queries'
import { useRosterSubscription } from '@/hooks/use-arc-socket'
import { initials } from '@/lib/format'
import type { Agent } from '@/lib/types'

function AgentCard({ agent, onOpen }: { agent: Agent; onOpen: () => void }) {
  const label = agent.display_name || agent.name || agent.agent_id || 'unknown'
  return (
    <button
      type="button"
      onClick={onOpen}
      className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4 text-left shadow-xs transition-colors hover:border-primary/40 hover:bg-muted/30"
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
        <StatusDot online={agent.online} degraded={agent.degraded} />
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
    </button>
  )
}

export function AgentsPage() {
  const navigate = useNavigate()
  const query = useRoster()
  const agents = (query.data?.agents ?? []).filter((a) => !a.hidden)
  useRosterSubscription(agents.map((a) => a.agent_id ?? '').filter(Boolean))

  const online = agents.filter((a) => a.online).length
  const degraded = agents.filter((a) => a.degraded).length
  const models = new Set(agents.map((a) => a.model).filter(Boolean)).size

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Agents" description="The agent fleet and live status." />
      <div className="flex-1 space-y-5 overflow-auto p-6">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard label="Agents" value={agents.length} icon={<Boxes className="size-4" />} />
          <StatCard label="Online" value={online} />
          <StatCard label="Degraded" value={degraded} />
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
