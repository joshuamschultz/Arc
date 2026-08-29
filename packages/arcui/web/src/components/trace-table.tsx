import { useMemo, useState } from 'react'
import type { ColumnDef } from '@tanstack/react-table'
import { Download } from 'lucide-react'
import { DataTable } from '@/components/data-table'
import { TraceDrawer } from '@/components/trace-drawer'
import { StatusText } from '@/components/status-badge'
import { AgentIdentity } from '@/components/AgentIdentity'
import { CapabilityBadge } from '@/components/llm/capability-badge'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { fmtCost, fmtLatency, fmtNumber, jobLabel, relativeTime, shortId } from '@/lib/format'
import type { AgentIdentityShape, Trace } from '@/lib/types'

/** A trace's identity when the roster join didn't attach one (e.g. an older
 * route) — still renders the DID/label the call actually carried instead of
 * an empty block (H-007 pattern; see agent-detail's `ParticipantIdentity`). */
function fallbackIdentity(t: Trace): AgentIdentityShape {
  return {
    did: t.agent || '',
    host: 'unknown',
    platform: 'unknown',
    type: 'unknown',
    short_id: 'unknown',
    name: t.agent_label ?? null,
  }
}

const columns: ColumnDef<Trace, unknown>[] = [
  {
    accessorKey: 'trace_id',
    header: 'Trace',
    cell: (c) => (
      <span className="inline-flex items-center rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-primary">
        {shortId(c.getValue() as string)}
      </span>
    ),
  },
  {
    accessorFn: (r) => r.agent_label || r.agent,
    id: 'agent',
    header: 'Agent',
    // H-007/H-029: the canonical identity block (friendly name + type + DID),
    // resolved server-side and joined by DID (H-008) — never the raw label.
    cell: (c) => {
      const t = c.row.original
      const job = jobLabel(t.job ?? null)
      return (
        <div className="flex flex-col gap-1">
          <AgentIdentity
            identity={t.identity ?? fallbackIdentity(t)}
            fallbackName={t.agent_label || undefined}
            size="sm"
            showAvatar={false}
          />
          {job && (
            <span
              className="truncate text-[11px] leading-tight text-foreground/55"
              title="A background job the agent ran on its own (not a person-driven call)"
            >
              {job}
            </span>
          )}
        </div>
      )
    },
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
    accessorKey: 'capability_class',
    id: 'type',
    header: 'Type',
    // H-028/H-029: what the call WAS, plus its short operation label
    // (embed:*/retrieve:*) when the embed path stamped one.
    cell: (c) => {
      const t = c.row.original
      return (
        <div className="flex flex-col gap-0.5">
          <CapabilityBadge value={t.capability_class} />
          {t.operation && (
            <span className="truncate font-mono text-[10px] text-muted-foreground">{t.operation}</span>
          )}
        </div>
      )
    },
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

const ALL = '__all__'
const EXPORT_FIELDS = [
  'trace_id', 'agent', 'capability_class', 'operation', 'job', 'model', 'provider',
  'input_tokens', 'output_tokens', 'duration_ms', 'cost_usd', 'status', 'timestamp',
] as const

function uniq(values: Array<string | undefined>): string[] {
  return [...new Set(values.filter((v): v is string => !!v))].sort()
}

function toCsv(rows: Trace[]): string {
  const head = EXPORT_FIELDS.join(',')
  const body = rows.map((t) =>
    EXPORT_FIELDS.map((f) => {
      const v = f === 'agent' ? (t.agent_label ?? t.agent ?? '') : (t[f] ?? '')
      const s = String(v).replace(/"/g, '""')
      return /[",\n]/.test(s) ? `"${s}"` : s
    }).join(','),
  )
  return [head, ...body].join('\n')
}

function downloadCsv(rows: Trace[]) {
  const blob = new Blob([toCsv(rows)], { type: 'text/csv' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'traces.csv'
  a.click()
  URL.revokeObjectURL(url)
}

function FilterSelect({
  value, onChange, options, placeholder,
}: {
  value: string
  onChange: (v: string) => void
  options: string[]
  placeholder: string
}) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className="h-8 w-auto min-w-28 text-xs">
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={ALL}>{placeholder}</SelectItem>
        {options.map((o) => (
          <SelectItem key={o} value={o}>{o}</SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

/** Sortable, searchable LLM-call table with column filters, CSV export, and a
 *  per-row detail drawer. */
export function TraceTable({ traces }: { traces: Trace[] }) {
  const [active, setActive] = useState<Trace | null>(null)
  const [provider, setProvider] = useState(ALL)
  const [model, setModel] = useState(ALL)
  const [agent, setAgent] = useState(ALL)
  const [status, setStatus] = useState(ALL)
  const [type, setType] = useState(ALL)

  const providers = useMemo(() => uniq(traces.map((t) => t.provider)), [traces])
  const models = useMemo(() => uniq(traces.map((t) => t.model)), [traces])
  const agents = useMemo(() => uniq(traces.map((t) => t.agent_label || t.agent)), [traces])
  const statuses = useMemo(() => uniq(traces.map((t) => t.status || 'ok')), [traces])
  // H-028 classes only — never derived from the model name (see CapabilityBadge).
  const types = useMemo(() => uniq(traces.map((t) => t.capability_class || 'inference')), [traces])

  const filtered = useMemo(
    () =>
      traces.filter((t) => {
        if (provider !== ALL && t.provider !== provider) return false
        if (model !== ALL && t.model !== model) return false
        if (agent !== ALL && (t.agent_label || t.agent) !== agent) return false
        if (status !== ALL && (t.status || 'ok') !== status) return false
        if (type !== ALL && (t.capability_class || 'inference') !== type) return false
        return true
      }),
    [traces, provider, model, agent, status, type],
  )

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <FilterSelect value={provider} onChange={setProvider} options={providers} placeholder="All providers" />
        <FilterSelect value={model} onChange={setModel} options={models} placeholder="All models" />
        <FilterSelect value={agent} onChange={setAgent} options={agents} placeholder="All agents" />
        <FilterSelect value={status} onChange={setStatus} options={statuses} placeholder="All status" />
        <FilterSelect value={type} onChange={setType} options={types} placeholder="All types" />
        <Button
          variant="outline"
          size="sm"
          className="ml-auto"
          disabled={filtered.length === 0}
          onClick={() => downloadCsv(filtered)}
        >
          <Download className="size-4" /> Export
        </Button>
      </div>
      <DataTable
        columns={columns}
        data={filtered}
        searchable
        searchPlaceholder="Search calls…"
        onRowClick={setActive}
        isRowActive={(t) => t.trace_id === active?.trace_id}
        emptyTitle="No LLM calls yet"
        emptyDescription="Calls appear here live as agents make LLM requests."
      />
      <TraceDrawer trace={active} open={!!active} onOpenChange={(o) => !o && setActive(null)} />
    </div>
  )
}
