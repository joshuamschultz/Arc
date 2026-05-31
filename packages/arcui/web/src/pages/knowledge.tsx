import { useState } from 'react'
import { BookOpen } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { StatCard } from '@/components/stat-card'
import { JsonBlock } from '@/components/json-block'
import { FileTree } from '@/components/file-tree'
import { QueryState, EmptyState } from '@/components/states'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useKnowledge, useRoster } from '@/lib/queries'

/** Render a flat dict of scalars as stat tiles; fall back to JSON. */
function ScalarStats({ data }: { data?: Record<string, unknown> }) {
  if (!data) return null
  const scalars = Object.entries(data).filter(
    ([, v]) => typeof v === 'number' || typeof v === 'string' || typeof v === 'boolean',
  )
  if (scalars.length === 0) return <JsonBlock value={data} />
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
      {scalars.map(([k, v]) => (
        <StatCard key={k} label={k.replace(/_/g, ' ')} value={String(v)} />
      ))}
    </div>
  )
}

export function KnowledgePage() {
  const roster = useRoster()
  const agents = (roster.data?.agents ?? []).filter((a) => !a.hidden)
  const [picked, setPicked] = useState<string | null>(null)
  const agentId = picked ?? agents[0]?.agent_id ?? null
  const setAgentId = setPicked

  const query = useKnowledge(agentId)

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Knowledge"
        description="Per-agent context budget, memory, workspace, and graph."
        actions={
          <Select value={agentId ?? ''} onValueChange={setAgentId}>
            <SelectTrigger className="w-52">
              <SelectValue placeholder="Select agent" />
            </SelectTrigger>
            <SelectContent>
              {agents.map((a) => (
                <SelectItem key={a.agent_id} value={a.agent_id ?? ''}>
                  {a.display_name || a.name || a.agent_id}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        }
      />
      <div className="flex-1 space-y-6 overflow-auto p-6">
        {!agentId ? (
          <EmptyState
            icon={<BookOpen className="size-7" />}
            title="No agent selected"
            description="Register an agent and pick it above to inspect its knowledge."
          />
        ) : (
          <QueryState query={query} isEmpty={() => !query.data}>
            {(data) => (
              <>
                <section className="space-y-2">
                  <h2 className="text-sm font-semibold text-foreground">Context budget</h2>
                  <ScalarStats data={data.context} />
                </section>
                <section className="space-y-2">
                  <h2 className="text-sm font-semibold text-foreground">Graph</h2>
                  <ScalarStats data={data.graph} />
                </section>
                <section className="space-y-2">
                  <h2 className="text-sm font-semibold text-foreground">Memory</h2>
                  <JsonBlock value={data.memory ?? {}} className="max-h-72" />
                </section>
                <section className="space-y-2">
                  <h2 className="text-sm font-semibold text-foreground">Workspace</h2>
                  <FileTree agentId={agentId} />
                </section>
              </>
            )}
          </QueryState>
        )}
      </div>
    </div>
  )
}
