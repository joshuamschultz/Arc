import { useMemo } from 'react'
import type { ColumnDef } from '@tanstack/react-table'
import { Wrench, Sparkles } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { DataTable } from '@/components/data-table'
import { StatCard } from '@/components/stat-card'
import { QueryState, EmptyState } from '@/components/states'
import { CapabilityTable } from '@/components/capability-table'
import { ClassificationBadge } from '@/components/tools-table'
import { useTeamToolsSkills } from '@/lib/queries'
import type { CapabilityInventoryItem, Dict } from '@/lib/types'

interface FleetSkillRow extends CapabilityInventoryItem {
  agent_id?: string
}

interface ToolRow extends Dict {
  name?: string
  agents?: string[]
  classification?: string
}

const toolColumns: ColumnDef<ToolRow, unknown>[] = [
  {
    accessorKey: 'name',
    header: 'Tool',
    cell: (c) => <span className="font-mono text-xs text-foreground">{String(c.getValue() ?? '—')}</span>,
  },
  {
    accessorKey: 'classification',
    header: 'Classification',
    cell: (c) => <ClassificationBadge value={c.getValue() as string} />,
  },
  {
    id: 'count',
    header: 'Agents',
    accessorFn: (r) => (Array.isArray(r.agents) ? r.agents.length : 0),
    cell: (c) => <span className="tabular-nums text-muted-foreground">{c.getValue() as number}</span>,
  },
  {
    id: 'agents',
    header: 'Available on',
    cell: (c) => {
      const agents = (c.row.original.agents ?? []) as string[]
      return (
        <div className="flex flex-wrap gap-1">
          {agents.map((a) => (
            <span key={a} className="rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
              {a}
            </span>
          ))}
        </div>
      )
    },
  },
]

export function ToolsSkillsPage() {
  const query = useTeamToolsSkills()
  const tools = useMemo(() => (query.data?.tools ?? []) as ToolRow[], [query.data])
  const skills = useMemo(() => (query.data?.skills ?? []) as unknown as FleetSkillRow[], [query.data])

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Tools & Skills" description="Fleet capability matrix and skill directory." />
      <div className="flex-1 space-y-6 overflow-auto p-6">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard label="Tools" value={tools.length} icon={<Wrench className="size-4" />} />
          <StatCard label="Skills" value={skills.length} icon={<Sparkles className="size-4" />} />
        </div>

        <section className="space-y-2">
          <h2 className="text-sm font-semibold text-foreground">Tools</h2>
          <QueryState query={query} isEmpty={() => tools.length === 0}
            empty={<EmptyState icon={<Wrench className="size-7" />} title="No tools registered" />}>
            {() => <DataTable columns={toolColumns} data={tools} searchable searchPlaceholder="Search tools…" />}
          </QueryState>
        </section>

        <section className="space-y-2">
          <h2 className="text-sm font-semibold text-foreground">Skills</h2>
          <QueryState query={query} isEmpty={() => skills.length === 0}
            empty={<EmptyState icon={<Sparkles className="size-7" />} title="No skills registered" />}>
            {() => (
              <CapabilityTable
                items={skills}
                agentAccessor={(row) => row.agent_id}
                searchPlaceholder="Search skills…"
                emptyTitle="No skills registered"
              />
            )}
          </QueryState>
        </section>
      </div>
    </div>
  )
}
