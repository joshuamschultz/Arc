import { useMemo, useState } from 'react'
import type { ColumnDef } from '@tanstack/react-table'
import { PageHeader } from '@/components/page-header'
import { DataTable } from '@/components/data-table'
import { FilterPills } from '@/components/filter-pills'
import { StatusText } from '@/components/status-badge'
import { QueryState } from '@/components/states'
import { useTeamTasks } from '@/lib/queries'
import type { Task } from '@/lib/types'

const FILTERS = ['all', 'pending', 'in_progress', 'completed', 'failed']

const columns: ColumnDef<Task, unknown>[] = [
  {
    accessorKey: 'agent_id',
    header: 'Agent',
    cell: (c) => <span className="font-mono text-xs text-muted-foreground">{String(c.getValue() ?? '—')}</span>,
  },
  {
    accessorKey: 'id',
    header: 'ID',
    cell: (c) => <span className="font-mono text-xs">{String(c.getValue() ?? '—')}</span>,
  },
  {
    accessorKey: 'subject',
    header: 'Subject',
    cell: (c) => <span className="text-foreground">{String(c.getValue() ?? '—')}</span>,
  },
  {
    accessorKey: 'status',
    header: 'Status',
    cell: (c) => <StatusText value={c.getValue() as string} />,
  },
  {
    accessorKey: 'owner',
    header: 'Owner',
    cell: (c) => <span className="text-muted-foreground">{String(c.getValue() ?? '—')}</span>,
  },
]

export function TasksPage() {
  const query = useTeamTasks()
  const [filter, setFilter] = useState('all')
  const tasks = useMemo(() => query.data?.tasks ?? [], [query.data])

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: tasks.length }
    for (const t of tasks) {
      const s = (t.status || 'unknown').toLowerCase()
      c[s] = (c[s] ?? 0) + 1
    }
    return c
  }, [tasks])

  const filtered = filter === 'all' ? tasks : tasks.filter((t) => (t.status || '').toLowerCase() === filter)

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Tasks" description="Fleet-wide task queue across all agents." />
      <div className="flex-1 space-y-4 overflow-auto p-6">
        <FilterPills
          value={filter}
          onChange={setFilter}
          options={FILTERS.map((f) => ({
            value: f,
            label: f === 'all' ? 'All' : f.replace(/_/g, ' '),
            count: counts[f] ?? 0,
          }))}
        />
        <QueryState query={query} isEmpty={() => tasks.length === 0} empty={
          <div className="rounded-xl border border-dashed border-border bg-card/30 p-10 text-center text-sm text-muted-foreground">
            No tasks across the fleet yet.
          </div>
        }>
          {() => (
            <DataTable
              columns={columns}
              data={filtered}
              searchable
              searchPlaceholder="Search tasks…"
              emptyTitle="No matching tasks"
            />
          )}
        </QueryState>
      </div>
    </div>
  )
}
