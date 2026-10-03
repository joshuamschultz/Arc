import { type ReactNode } from 'react'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { FieldHelp } from '@/components/help'
import { useConnectedSources } from '@/lib/queries'

// Small pieces the Knowledge connection views and the profile-review panel share.

const TH_CLASS =
  'px-3 py-2 text-left text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground'

export function Th({ children }: { children: ReactNode }) {
  return <th className={TH_CLASS}>{children}</th>
}

export function Chip({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-full border border-border bg-muted/40 px-2 py-0.5 text-xs text-muted-foreground">
      {children}
    </span>
  )
}

export function MonoChip({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
      {children}
    </span>
  )
}

/** A picker over an agent's connector sources. Value is the raw source id. */
export function SourceSelect({
  agentId,
  value,
  onChange,
  placeholder = 'Select source',
}: {
  agentId: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
}) {
  const sources = useConnectedSources(agentId)
  const items = sources.data?.items ?? []
  return (
    <div className="flex items-center gap-1"><Select value={value} onValueChange={onChange}>
      <SelectTrigger className="w-56 max-w-full">
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {/* A source whose inspection failed carries no source_id, and every call
            below is keyed by it. Radix also throws on an empty Select value, so an
            unaddressable source is left out of the picker rather than crashing the
            page — the connection card still shows it, with its failure. */}
        {items
          .filter((s) => Boolean(s.source_id))
          .map((s) => (
            <SelectItem key={s.connection_id} value={s.source_id}>
              {s.label || s.connection_id}
            </SelectItem>
          ))}
      </SelectContent>
    </Select><FieldHelp helpKey="knowledge.source" route="knowledge" /></div>
  )
}

