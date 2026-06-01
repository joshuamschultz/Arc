import { useMemo, useState } from 'react'
import type { ColumnDef } from '@tanstack/react-table'
import { Shield, ShieldAlert } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { DataTable } from '@/components/data-table'
import { FilterPills } from '@/components/filter-pills'
import { SeverityBadge } from '@/components/status-badge'
import { EventDrawer } from '@/components/event-drawer'
import { QueryState } from '@/components/states'
import { useTeamAudit } from '@/lib/queries'
import { relativeTime } from '@/lib/format'
import type { AuditEvent } from '@/lib/types'

const FILTERS = [
  { value: 'all', label: 'All' },
  { value: 'control', label: 'Control' },
  { value: 'deny', label: 'Denials' },
]

const columns: ColumnDef<AuditEvent, unknown>[] = [
  {
    accessorKey: 'timestamp',
    header: 'Time',
    cell: (c) => <span className="whitespace-nowrap text-xs text-muted-foreground">{relativeTime(c.getValue() as string)}</span>,
  },
  {
    accessorFn: (r) => r.event_type || r.action,
    id: 'event',
    header: 'Event',
    cell: (c) => <span className="font-mono text-xs text-foreground">{String(c.getValue() ?? '—')}</span>,
  },
  {
    accessorKey: 'agent_id',
    header: 'Agent',
    cell: (c) => <span className="font-mono text-xs text-muted-foreground">{String(c.getValue() ?? '—')}</span>,
  },
  {
    accessorKey: 'actor',
    header: 'Actor',
    cell: (c) => <span className="text-xs text-muted-foreground">{String(c.getValue() ?? '—')}</span>,
  },
  {
    accessorFn: (r) => r.severity || r.decision,
    id: 'severity',
    header: 'Severity',
    cell: (c) => <SeverityBadge value={c.getValue() as string} />,
  },
]

export function SecurityPage() {
  const [filter, setFilter] = useState('all')
  const query = useTeamAudit(filter === 'all' ? undefined : filter, 100)
  const [active, setActive] = useState<AuditEvent | null>(null)

  const events = useMemo(() => query.data?.events ?? [], [query.data])

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Security" description="Audit trail, control actions, and policy denials." />
      <div className="flex-1 space-y-5 overflow-auto p-6">
        <div className="flex flex-wrap items-center gap-4 rounded-xl border border-border bg-card p-4 shadow-xs">
          <Shield className="size-5 text-primary" />
          <div className="text-sm text-muted-foreground">
            Audit events are fetched on demand from the REST API.
          </div>
        </div>

        <FilterPills value={filter} onChange={setFilter} options={FILTERS} />

        <QueryState query={query} isEmpty={() => events.length === 0} empty={
          <div className="rounded-xl border border-dashed border-border bg-card/30 p-10 text-center text-sm text-muted-foreground">
            No audit events recorded.
          </div>
        }>
          {() => (
            <DataTable
              columns={columns}
              data={events}
              searchable
              searchPlaceholder="Search audit log…"
              onRowClick={setActive}
              emptyTitle="No matching events"
            />
          )}
        </QueryState>
      </div>

      <EventDrawer
        open={!!active}
        onOpenChange={(o) => !o && setActive(null)}
        title={active?.event_type || active?.action || 'Audit event'}
        description={active?.timestamp ? relativeTime(active.timestamp) : undefined}
        payload={active ?? undefined}
      >
        {active?.severity && (
          <div className="flex items-center gap-2">
            <ShieldAlert className="size-4 text-muted-foreground" />
            <SeverityBadge value={active.severity} />
          </div>
        )}
      </EventDrawer>
    </div>
  )
}
