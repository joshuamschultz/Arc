import { useMemo } from 'react'
import type { ColumnDef } from '@tanstack/react-table'
import { Wrench, Sparkles } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { DataTable } from '@/components/data-table'
import { StatCard } from '@/components/stat-card'
import { QueryState, EmptyState } from '@/components/states'
import { useTeamToolsSkills } from '@/lib/queries'
import type { Dict } from '@/lib/types'

interface ToolRow extends Dict {
  name?: string
  agents?: string[]
}
interface SkillRow extends Dict {
  name?: string
  description?: string
  agent_id?: string
}

const toolColumns: ColumnDef<ToolRow, unknown>[] = [
  {
    accessorKey: 'name',
    header: 'Tool',
    cell: (c) => <span className="font-mono text-xs text-foreground">{String(c.getValue() ?? '—')}</span>,
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
  const skills = useMemo(() => (query.data?.skills ?? []) as SkillRow[], [query.data])

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
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {skills.map((s, i) => (
                  <div key={`${s.name}-${i}`} className="rounded-xl border border-border bg-card p-4 shadow-xs">
                    <div className="flex items-center gap-2">
                      <Sparkles className="size-4 text-primary" />
                      <span className="truncate font-medium text-foreground">{s.name || 'unnamed'}</span>
                    </div>
                    {s.agent_id && (
                      <div className="mt-1 font-mono text-[11px] text-muted-foreground">{s.agent_id}</div>
                    )}
                    {s.description && (
                      <p className="mt-2 line-clamp-3 text-xs text-muted-foreground">{s.description}</p>
                    )}
                  </div>
                ))}
              </div>
            )}
          </QueryState>
        </section>
      </div>
    </div>
  )
}
