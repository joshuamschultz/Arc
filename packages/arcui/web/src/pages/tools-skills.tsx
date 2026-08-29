import { useMemo, useState } from 'react'
import type { ColumnDef } from '@tanstack/react-table'
import { Wrench, Sparkles } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { DataTable } from '@/components/data-table'
import { StatCard } from '@/components/stat-card'
import { QueryState, EmptyState } from '@/components/states'
import { CapabilityTable } from '@/components/capability-table'
import { CapabilityImportPanel } from '@/components/capability-import-panel'
import { ClassificationBadge, SourceBadge } from '@/components/tools-table'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useRoster, useTeamToolsSkills } from '@/lib/queries'
import { cn } from '@/lib/utils'
import type { Agent, CapabilityInventoryItem, Dict } from '@/lib/types'

interface FleetSkillRow extends CapabilityInventoryItem {
  agent_id?: string
  source?: string
}

interface ToolRow extends Dict {
  name?: string
  agents?: string[]
  classification?: string
  source?: string
}

// H-031: the four fixed source buckets every tool/skill row resolves to
// (builtin / agent / extension / module) — the same set SourceBadge renders.
const SOURCE_FILTERS = ['builtin', 'agent', 'extension', 'module'] as const
type SourceFilter = (typeof SOURCE_FILTERS)[number]
type TypeFilter = 'all' | 'tools' | 'skills'

/** An agent's filter-menu label — the H-007 resolved identity name first,
 *  falling through to whatever the roster row carries, never a raw agent_id
 *  guess re-derived client-side. */
function agentLabel(agent: Agent): string {
  return agent.identity?.name || agent.display_name || agent.name || agent.agent_id || 'Unknown'
}

const toolColumns: ColumnDef<ToolRow, unknown>[] = [
  {
    accessorKey: 'name',
    header: 'Tool',
    cell: (c) => (
      <span className="rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-foreground">
        {String(c.getValue() ?? '—')}
      </span>
    ),
  },
  {
    accessorKey: 'source',
    header: 'Source',
    cell: (c) => <SourceBadge value={c.getValue() as string} />,
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
  const roster = useRoster()
  const agents = (roster.data?.agents ?? []).filter((a): a is Agent & { agent_id: string } => !a.hidden && Boolean(a.agent_id))

  const [agentFilter, setAgentFilter] = useState('all')
  const [typeFilter, setTypeFilter] = useState<TypeFilter>('all')
  const [sourceFilter, setSourceFilter] = useState<Set<SourceFilter>>(new Set(SOURCE_FILTERS))

  const toggleSource = (source: SourceFilter) => {
    setSourceFilter((current) => {
      const next = new Set(current)
      if (next.has(source)) next.delete(source)
      else next.add(source)
      // Every source cleared reads the same as every source kept — an empty
      // set is not "show nothing", it is "no source filter applied".
      return next.size === 0 ? new Set(SOURCE_FILTERS) : next
    })
  }

  const allTools = useMemo(() => (query.data?.tools ?? []) as ToolRow[], [query.data])
  const allSkills = useMemo(() => (query.data?.skills ?? []) as unknown as FleetSkillRow[], [query.data])

  const tools = useMemo(
    () =>
      allTools.filter(
        (t) =>
          (agentFilter === 'all' || (t.agents ?? []).includes(agentFilter)) &&
          sourceFilter.has((t.source as SourceFilter) ?? 'agent'),
      ),
    [allTools, agentFilter, sourceFilter],
  )
  const skills = useMemo(
    () =>
      allSkills.filter(
        (s) =>
          (agentFilter === 'all' || s.agent_id === agentFilter) &&
          sourceFilter.has((s.source as SourceFilter) ?? 'agent'),
      ),
    [allSkills, agentFilter, sourceFilter],
  )

  const showTools = typeFilter !== 'skills'
  const showSkills = typeFilter !== 'tools'
  const filtersActive =
    agentFilter !== 'all' || typeFilter !== 'all' || sourceFilter.size !== SOURCE_FILTERS.length

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Tools & Skills" description="Fleet capability matrix and skill directory." />
      <div className="flex-1 space-y-6 overflow-auto p-6">
        <CapabilityImportPanel />
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard label="Tools" value={tools.length} icon={<Wrench className="size-4" />} />
          <StatCard label="Skills" value={skills.length} icon={<Sparkles className="size-4" />} />
        </div>

        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-card p-3">
          <label className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
            Agent
            <Select value={agentFilter} onValueChange={setAgentFilter}>
              <SelectTrigger size="sm" className="h-8 min-w-36 text-xs">
                <SelectValue placeholder="All agents" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All agents</SelectItem>
                {agents.map((a) => (
                  <SelectItem key={a.agent_id} value={a.agent_id as string}>
                    {agentLabel(a)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>

          <div className="flex items-center gap-1 text-xs font-medium text-muted-foreground">
            Type
            <div className="flex overflow-hidden rounded-md border border-border">
              {(['all', 'tools', 'skills'] as const).map((value) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setTypeFilter(value)}
                  className={cn(
                    'px-2.5 py-1 text-xs capitalize transition-colors',
                    typeFilter === value
                      ? 'bg-primary text-primary-foreground'
                      : 'bg-transparent text-muted-foreground hover:bg-muted/60',
                  )}
                >
                  {value}
                </button>
              ))}
            </div>
          </div>

          <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
            Source
            {SOURCE_FILTERS.map((source) => (
              <button
                key={source}
                type="button"
                onClick={() => toggleSource(source)}
                className={cn('rounded-md transition-opacity', !sourceFilter.has(source) && 'opacity-30')}
                aria-pressed={sourceFilter.has(source)}
              >
                <SourceBadge value={source} />
              </button>
            ))}
          </div>

          {filtersActive && (
            <Button
              type="button"
              variant="ghost"
              size="xs"
              onClick={() => {
                setAgentFilter('all')
                setTypeFilter('all')
                setSourceFilter(new Set(SOURCE_FILTERS))
              }}
            >
              Clear filters
            </Button>
          )}
        </div>

        {showTools && (
          <section className="space-y-2">
            <h2 className="text-sm font-semibold text-foreground">Tools</h2>
            <QueryState query={query} isEmpty={() => tools.length === 0}
              empty={<EmptyState icon={<Wrench className="size-7" />} title="No tools match these filters" />}>
              {() => <DataTable columns={toolColumns} data={tools} searchable searchPlaceholder="Search tools…" />}
            </QueryState>
          </section>
        )}

        {showSkills && (
          <section className="space-y-2">
            <h2 className="text-sm font-semibold text-foreground">Skills</h2>
            <QueryState query={query} isEmpty={() => skills.length === 0}
              empty={<EmptyState icon={<Sparkles className="size-7" />} title="No skills match these filters" />}>
              {() => (
                <CapabilityTable
                  items={skills}
                  agentAccessor={(row) => row.agent_id}
                  searchPlaceholder="Search skills…"
                  emptyTitle="No skills match these filters"
                />
              )}
            </QueryState>
          </section>
        )}
      </div>
    </div>
  )
}
