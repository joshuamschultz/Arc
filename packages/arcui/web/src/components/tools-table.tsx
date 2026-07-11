import { Fragment, useMemo, useState } from 'react'
import { ChevronRight } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import type { Dict } from '@/lib/types'

const CLASS_LABEL: Record<string, string> = {
  read_only: 'read-only',
  state_modifying: 'write',
  external_effect: 'external',
}
const CLASS_TONE: Record<string, string> = {
  read_only: 'bg-status-online/15 text-status-online',
  state_modifying: 'bg-status-warning/15 text-status-warning',
  external_effect: 'bg-status-error/15 text-status-error',
}

/** read-only / write / external badge — the tool's effect classification. */
export function ClassificationBadge({ value }: { value?: string }) {
  if (!value) return <span className="text-xs text-muted-foreground">—</span>
  return (
    <span className={cn('rounded-full px-2 py-0.5 text-[11px] font-medium', CLASS_TONE[value] ?? 'bg-muted text-muted-foreground')}>
      {CLASS_LABEL[value] ?? value}
    </span>
  )
}

function StatusBadge({ value }: { value?: string }) {
  const tone =
    value === 'deny'
      ? 'bg-status-error/15 text-status-error'
      : value === 'inactive'
        ? 'bg-muted text-muted-foreground'
        : 'bg-status-online/15 text-status-online'
  return <span className={cn('rounded-full px-2 py-0.5 text-[11px] font-medium', tone)}>{value ?? 'allow'}</span>
}

/** Tool surface table: transport, effect classification, allow/deny status, and
 *  a click-to-expand description row. Row click also opens the detail drawer
 *  (U6) when `onRowClick` is supplied — the inline expand stays for a quick
 *  glance without leaving the table. */
export function ToolsTable({
  tools,
  onRowClick,
}: {
  tools: Dict[]
  onRowClick?: (tool: Dict) => void
}) {
  const [open, setOpen] = useState<number | null>(null)
  const [filter, setFilter] = useState('')
  const rows = useMemo(() => {
    const q = filter.toLowerCase()
    return tools.filter((t) =>
      !q || `${t.name ?? ''} ${t.transport ?? ''} ${t.classification ?? ''}`.toLowerCase().includes(q),
    )
  }, [tools, filter])

  return (
    <div className="space-y-2">
      <Input
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        placeholder="Filter tools…"
        className="h-8 max-w-xs"
      />
      <div className="overflow-hidden rounded-xl border border-border bg-card">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-[11px] uppercase tracking-wide text-muted-foreground">
              <th className="w-6 px-3 py-2" />
              <th className="px-3 py-2">Tool</th>
              <th className="px-3 py-2">Transport</th>
              <th className="px-3 py-2">Classification</th>
              <th className="px-3 py-2">Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((t, i) => {
              const inactive = t.status === 'inactive'
              const desc = String(t.description ?? '')
              const isOpen = open === i
              return (
                <Fragment key={`${t.name}-${i}`}>
                  <tr
                    className={cn('cursor-pointer border-b border-border/60 last:border-0 hover:bg-muted/40', inactive && 'opacity-50')}
                    onClick={() => {
                      setOpen(isOpen ? null : i)
                      onRowClick?.(t)
                    }}
                  >
                    <td className="px-3 py-2 text-muted-foreground">
                      {desc && <ChevronRight className={cn('size-3.5 transition-transform', isOpen && 'rotate-90')} />}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs text-foreground">{String(t.name ?? '—')}</td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">{String(t.transport || '—')}</td>
                    <td className="px-3 py-2"><ClassificationBadge value={t.classification as string} /></td>
                    <td className="px-3 py-2"><StatusBadge value={t.status as string} /></td>
                  </tr>
                  {isOpen && desc && (
                    <tr className="border-b border-border/60 bg-muted/20">
                      <td />
                      <td colSpan={4} className="px-3 py-2 text-xs text-muted-foreground">{desc}</td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
