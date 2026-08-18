import { useNavigate } from 'react-router-dom'
import { Boxes, Wifi, Moon, Cpu } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { RestartGatewayButton } from '@/components/restart-gateway-button'
import { InsightStat } from '@/components/ai'
import { QueryState, EmptyState } from '@/components/states'
import { AgentCard } from '@/components/fleet/agent-card'
import { useRoster } from '@/lib/queries'

export function AgentsPage() {
  const navigate = useNavigate()
  const query = useRoster()
  const agents = (query.data?.agents ?? []).filter((a) => !a.hidden)

  const online = agents.filter((a) => a.online).length
  const idle = agents.length - online
  const models = new Set(agents.map((a) => a.model).filter(Boolean)).size

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Fleet"
        description="The agents working for you, and what each is doing right now."
        actions={<RestartGatewayButton />}
      />
      <div className="flex-1 space-y-5 overflow-auto p-6">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <InsightStat label="Agents" value={agents.length} icon={<Boxes className="size-4" />} />
          <InsightStat label="Online" value={online} icon={<Wifi className="size-4" />} />
          <InsightStat label="Idle" value={idle} icon={<Moon className="size-4" />} />
          <InsightStat label="Models" value={models} icon={<Cpu className="size-4" />} />
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
              {agents.map((agent, i) => (
                <div
                  key={agent.agent_id}
                  className="animate-in fade-in-0 slide-in-from-bottom-2 fill-mode-both duration-300"
                  style={{ animationDelay: `${Math.min(i, 8) * 40}ms` }}
                >
                  <AgentCard
                    agent={agent}
                    onOpen={() => navigate(`/agents/${agent.agent_id}`)}
                  />
                </div>
              ))}
            </div>
          )}
        </QueryState>
      </div>
    </div>
  )
}
