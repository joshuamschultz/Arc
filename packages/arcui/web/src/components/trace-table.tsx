import { useState } from 'react'
import type { ColumnDef } from '@tanstack/react-table'
import { DataTable } from '@/components/data-table'
import { TraceDrawer } from '@/components/trace-drawer'
import { StatusText } from '@/components/status-badge'
import { fmtCost, fmtLatency, fmtNumber, relativeTime, shortId } from '@/lib/format'
import type { Trace } from '@/lib/types'

const columns: ColumnDef<Trace, unknown>[] = [
  {
    accessorKey: 'trace_id',
    header: 'Trace',
    cell: (c) => <span className="font-mono text-xs text-primary">{shortId(c.getValue() as string)}</span>,
  },
  {
    accessorFn: (r) => r.agent_label || r.agent,
    id: 'agent',
    header: 'Agent',
    cell: (c) => <span className="text-xs text-foreground">{String(c.getValue() ?? '—')}</span>,
  },
  {
    accessorKey: 'model',
    header: 'Model',
    cell: (c) => <span className="font-mono text-xs text-muted-foreground">{String(c.getValue() ?? '—')}</span>,
  },
  {
    accessorKey: 'provider',
    header: 'Provider',
    cell: (c) => <span className="text-xs text-muted-foreground">{String(c.getValue() ?? '—')}</span>,
  },
  {
    accessorFn: (r) => r.input_tokens ?? r.total_tokens ?? 0,
    id: 'in',
    header: 'In',
    cell: (c) => <span className="font-mono text-xs tabular-nums">{fmtNumber(c.getValue() as number)}</span>,
  },
  {
    accessorKey: 'output_tokens',
    header: 'Out',
    cell: (c) => <span className="font-mono text-xs tabular-nums">{fmtNumber(c.getValue() as number)}</span>,
  },
  {
    accessorKey: 'duration_ms',
    header: 'Latency',
    cell: (c) => <span className="font-mono text-xs tabular-nums">{fmtLatency(c.getValue() as number)}</span>,
  },
  {
    accessorKey: 'cost_usd',
    header: 'Cost',
    cell: (c) => <span className="font-mono text-xs tabular-nums">{fmtCost(c.getValue() as number)}</span>,
  },
  {
    accessorKey: 'status',
    header: 'Status',
    cell: (c) => <StatusText value={(c.getValue() as string) || 'ok'} />,
  },
  {
    accessorKey: 'timestamp',
    header: 'Time',
    cell: (c) => <span className="whitespace-nowrap text-xs text-muted-foreground">{relativeTime(c.getValue() as string)}</span>,
  },
]

/** Sortable, searchable LLM-call table with a per-row detail drawer. */
export function TraceTable({ traces }: { traces: Trace[] }) {
  const [active, setActive] = useState<Trace | null>(null)
  return (
    <>
      <DataTable
        columns={columns}
        data={traces}
        searchable
        searchPlaceholder="Search calls…"
        onRowClick={setActive}
        isRowActive={(t) => t.trace_id === active?.trace_id}
        emptyTitle="No LLM calls yet"
        emptyDescription="Calls appear here live as agents make LLM requests."
      />
      <TraceDrawer trace={active} open={!!active} onOpenChange={(o) => !o && setActive(null)} />
    </>
  )
}
