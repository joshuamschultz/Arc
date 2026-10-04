import { Link } from 'react-router-dom'
import { History } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { EmptyState, QueryState } from '@/components/states'
import { SectionCard } from '@/components/maintenance/section-card'
import { useRoster } from '@/lib/queries'

/** A pointer, not a copy. Every saved change to an agent's prompts is kept as a signed
 *  version, and each agent's Prompts tab already shows the history, a side-by-side
 *  difference between any two versions, and a restore. This card only lists where to go. */
export function PromptHistorySection() {
  const roster = useRoster()
  return (
    <SectionCard
      title="Prompt history"
      description="Every change to an agent's prompts is kept and signed. Open an agent's Prompts tab, choose a prompt, then History to compare versions or restore an earlier one."
    >
      <QueryState
        query={roster}
        isEmpty={(data) => data.agents.filter((a) => !a.hidden).length === 0}
        empty={<EmptyState title="No agents yet" description="Prompt history appears once an agent exists." />}
      >
        {(data) => (
          <ul className="flex flex-wrap gap-2">
            {data.agents
              .filter((a) => !a.hidden && a.agent_id)
              .map((a) => (
                <li key={a.agent_id}>
                  <Button asChild size="sm" variant="outline">
                    <Link to={`/agents/${a.agent_id}/prompts`}>
                      <History />
                      {a.display_name || a.name || a.agent_id}
                    </Link>
                  </Button>
                </li>
              ))}
          </ul>
        )}
      </QueryState>
    </SectionCard>
  )
}
