import type { ColumnDef } from '@tanstack/react-table'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { DataTable } from '@/components/data-table'
import { cn } from '@/lib/utils'
import type { CapabilityInventoryItem } from '@/lib/types'

/** Source-root family badge — color groups by root, but the label is always
 *  the loader's own verbatim root name (e.g. "workspace-skills"), never a
 *  client-invented label, so a new scan root renders correctly untouched. */
export function SourceRootBadge({ value }: { value?: string | null }) {
  const root = value ?? '' // dynamic builtins (create-tool/skill…) carry no root
  const tone = root.startsWith('workspace')
    ? 'border-status-online/30 bg-status-online/15 text-status-online'
    : root.startsWith('agent')
      ? 'border-status-warning/30 bg-status-warning/15 text-status-warning'
      : root.startsWith('global')
        ? 'border-primary/30 bg-primary/15 text-primary'
        : 'border-border bg-muted text-muted-foreground' // builtins*
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-md border px-1.5 py-0.5 font-mono text-[11px] font-medium',
        tone,
      )}
    >
      {root || '—'}
    </span>
  )
}

/** Loader/TOFU verdict badge, rendered VERBATIM (REQ-093/096) — no enum, no
 *  translation. Color is a presentational heuristic on the string itself so a
 *  verdict the loader hasn't invented yet still reads as "needs a look"
 *  instead of silently blending in as fine. `detail` (when non-empty) surfaces
 *  in a tooltip — the load-error popover COMP-009 calls for. */
export function CapabilityStatusBadge({ status, detail }: { status?: string | null; detail?: string }) {
  const s = (status ?? '').toLowerCase() // dynamic builtins may carry no verdict
  const tone = s.includes('load')
    ? 'border-status-online/30 bg-status-online/15 text-status-online'
    : s.includes('deny') || s.includes('invalid') || s.includes('error')
      ? 'border-status-error/30 bg-status-error/15 text-status-error'
      : 'border-status-warning/30 bg-status-warning/15 text-status-warning' // unsigned, new_sighting, or any future verdict
  const badge = (
    <span
      className={cn(
        'inline-flex items-center rounded-md border px-1.5 py-0.5 text-[11px] font-medium',
        tone,
      )}
    >
      {status || '—'}
    </span>
  )
  if (!detail) return badge
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="cursor-help underline decoration-dotted underline-offset-2">{badge}</span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">{detail}</TooltipContent>
    </Tooltip>
  )
}

/** Shared column set for a capability inventory table — used for the Skills
 *  tab, the Tools tab's loader-verdict section, and the fleet Tools & Skills
 *  page (with `agentColumn` for the fleet's per-row agent stamp). Internal to
 *  `CapabilityTable` below — not exported to keep this a components-only file. */
function capabilityColumns<T extends CapabilityInventoryItem>(opts?: {
  agentAccessor?: (row: T) => string | undefined
}): ColumnDef<T, unknown>[] {
  const cols: ColumnDef<T, unknown>[] = [
    {
      accessorKey: 'name',
      header: 'Name',
      cell: (c) => <span className="font-mono text-xs text-foreground">{c.getValue() as string}</span>,
    },
    {
      accessorKey: 'version',
      header: 'Version',
      cell: (c) => <span className="font-mono text-xs text-muted-foreground">{(c.getValue() as string) || '—'}</span>,
    },
    {
      accessorKey: 'source_root',
      header: 'Source',
      cell: (c) => <SourceRootBadge value={c.getValue() as string} />,
    },
    {
      accessorKey: 'status',
      header: 'Status',
      cell: (c) => <CapabilityStatusBadge status={c.getValue() as string} detail={c.row.original.status_detail} />,
    },
    {
      accessorKey: 'description',
      header: 'Description',
      cell: (c) => <span className="text-xs text-muted-foreground">{(c.getValue() as string) || '—'}</span>,
    },
  ]
  if (opts?.agentAccessor) {
    cols.push({
      id: 'agent',
      header: 'Agent',
      accessorFn: opts.agentAccessor,
      cell: (c) => <span className="font-mono text-[11px] text-muted-foreground">{(c.getValue() as string) || '—'}</span>,
    })
  }
  return cols
}

/** Filterable table of capability inventory items (skills or tools). */
export function CapabilityTable<T extends CapabilityInventoryItem>({
  items,
  agentAccessor,
  searchPlaceholder = 'Search…',
  emptyTitle = 'Nothing here',
  onRowClick,
}: {
  items: T[]
  agentAccessor?: (row: T) => string | undefined
  searchPlaceholder?: string
  emptyTitle?: string
  /** Optional row-click handler — e.g. opening a detail drawer (U5). Omit for
   *  a plain read-only table (the fleet Tools & Skills page's usage). */
  onRowClick?: (row: T) => void
}) {
  return (
    <DataTable
      columns={capabilityColumns<T>({ agentAccessor })}
      data={items}
      searchable
      searchPlaceholder={searchPlaceholder}
      emptyTitle={emptyTitle}
      onRowClick={onRowClick}
    />
  )
}
