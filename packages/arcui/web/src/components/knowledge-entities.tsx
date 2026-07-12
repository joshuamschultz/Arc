import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { EmptyState, QueryState } from '@/components/states'
import { useEntities, useEntityLinks } from '@/lib/queries'
import { cn } from '@/lib/utils'
import type { EntityRecord } from '@/lib/types'

// Mirrors arcmemory.stores.semantic._FACT_RE — a fact line renders as
// "- {predicate}: {value} {confidence} {date}" optionally suffixed with
// " | was: {was_value} {was_confidence}" (confidence compact, e.g. ".41" or
// "1"; date is YYYY-MM-DD). Parsing here is display-only — the backend fact
// format is never changed from this file.
const FACT_RE =
  /^-\s+(.+?):\s+(.+?)\s+(\.\d+|1)\s+(\d{4}-\d{2}-\d{2})(?:\s+\|\s+was:\s+(.+?)\s+(\.\d+|1))?$/

interface ParsedFact {
  predicate: string
  value: string
  confidence: string
  date: string
  was?: { value: string; confidence: string }
}

/** Parse one `format_fact` line; falls back to `null` (render raw) if it
 *  doesn't match the expected shape. */
function parseFact(line: string): ParsedFact | null {
  const m = FACT_RE.exec(line)
  if (!m) return null
  const [, predicate, value, confidence, date, wasValue, wasConfidence] = m
  return {
    predicate,
    value,
    confidence,
    date,
    was: wasValue ? { value: wasValue, confidence: wasConfidence } : undefined,
  }
}

/** One fact rendered as predicate label + readable value, with
 *  confidence/date as subtle right-aligned metadata and an optional prior
 *  value below. */
function FactRow({ fact }: { fact: string }) {
  const parsed = parseFact(fact)
  if (!parsed) {
    return (
      <li className="rounded-lg border border-border bg-muted/20 px-3 py-2 text-sm text-foreground">
        {fact}
      </li>
    )
  }
  return (
    <li className="flex items-start justify-between gap-3 rounded-lg border border-border bg-muted/20 px-3 py-2">
      <div className="min-w-0 space-y-1">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="rounded-sm border border-border bg-muted/60 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
            {parsed.predicate.replace(/_/g, ' ')}
          </span>
          <span className="text-sm text-foreground">{parsed.value}</span>
        </div>
        {parsed.was && (
          <div className="text-xs text-muted-foreground">
            was: {parsed.was.value}{' '}
            <span className="font-mono text-[11px]">{parsed.was.confidence}</span>
          </div>
        )}
      </div>
      <div className="shrink-0 text-right text-[11px] text-muted-foreground">
        <div className="font-mono tabular-nums">{parsed.confidence}</div>
        <div className="tabular-nums">{parsed.date}</div>
      </div>
    </li>
  )
}

function ImportanceBadge({ n }: { n: number }) {
  const tone =
    n >= 8
      ? 'bg-status-online/15 text-status-online'
      : n >= 4
        ? 'bg-status-warning/15 text-status-warning'
        : 'bg-muted text-muted-foreground'
  return (
    <span className={cn('rounded-full px-2 py-0.5 font-mono text-[11px] tabular-nums', tone)}>
      {n}/10
    </span>
  )
}

/** Entity detail: facts, tags, and navigable links (entities are read-only —
 *  the backend exposes no entity mutation surface, only memory edits). */
function EntityDetail({
  agentId,
  entity,
  open,
  onOpenChange,
  onNavigateEntity,
}: {
  agentId: string
  entity: EntityRecord | null
  open: boolean
  onOpenChange: (o: boolean) => void
  onNavigateEntity: (slug: string) => void
}) {
  const links = useEntityLinks(agentId, entity?.slug ?? null)
  if (entity == null) return null

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-xl">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="text-sm">{entity.name}</SheetTitle>
          <SheetDescription>
            {entity.entity_type} · {entity.classification} · confidence{' '}
            <span className="font-mono tabular-nums">{entity.confidence.toFixed(2)}</span>
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 space-y-5 overflow-auto p-5">
          <section className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                Importance
              </div>
              <div className="mt-1">
                <ImportanceBadge n={entity.importance} />
              </div>
            </div>
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                Source
              </div>
              <div className="mt-1 truncate rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-foreground">
                {entity.source}
              </div>
            </div>
          </section>

          {entity.tags.length > 0 && (
            <section className="space-y-2">
              <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                Tags
              </h3>
              <div className="flex flex-wrap gap-1.5">
                {entity.tags.map((t) => (
                  <span key={t} className="rounded-full border border-border bg-muted/40 px-2 py-0.5 text-xs text-muted-foreground">
                    {t}
                  </span>
                ))}
              </div>
            </section>
          )}

          <section className="space-y-2">
            <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Facts{entity.facts.length ? ` (${entity.facts.length})` : ''}
            </h3>
            {entity.facts.length === 0 ? (
              <p className="text-xs text-muted-foreground">No recorded facts.</p>
            ) : (
              <ul className="space-y-1.5">
                {entity.facts.map((f, i) => (
                  <FactRow key={i} fact={f} />
                ))}
              </ul>
            )}
          </section>

          <section className="space-y-2">
            <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Links
            </h3>
            <QueryState
              query={links}
              isEmpty={(d) => d.items.length === 0}
              empty={<p className="text-xs text-muted-foreground">No linked nodes.</p>}
            >
              {(data) => (
                <div className="flex flex-wrap gap-1.5">
                  {data.items.map((l, i) =>
                    l.target_type === 'entity' ? (
                      <button
                        key={i}
                        type="button"
                        onClick={() => onNavigateEntity(l.target_id)}
                        className="cursor-pointer rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-xs text-primary transition-colors duration-150 hover:bg-primary/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
                      >
                        {l.target_id} · {l.kind} <span className="tabular-nums">({l.weight.toFixed(2)})</span>
                      </button>
                    ) : (
                      <span
                        key={i}
                        className="rounded-full border border-border bg-muted/40 px-2 py-0.5 text-xs text-muted-foreground"
                      >
                        {l.target_id} · {l.kind}
                      </span>
                    ),
                  )}
                </div>
              )}
            </QueryState>
          </section>
        </div>
      </SheetContent>
    </Sheet>
  )
}

/** Browse an agent's semantic entities and navigate their link graph (COMP-003).
 *  Selection is controlled by `selectedSlug`/`onSelectSlug` — both row clicks
 *  here and cross-tab navigation from a memory's links flow through the same
 *  parent-owned slug, so no effect is needed to sync an external "focus". */
export function EntityBrowser({
  agentId,
  selectedSlug,
  onSelectSlug,
}: {
  agentId: string
  selectedSlug: string | null
  onSelectSlug: (slug: string | null) => void
}) {
  const entities = useEntities(agentId)
  const selected = entities.data?.items.find((e) => e.slug === selectedSlug) ?? null

  return (
    <div className="space-y-3">
      <QueryState
        query={entities}
        isEmpty={(d) => d.items.length === 0}
        empty={
          <EmptyState
            title="No entities recorded yet"
            description="This agent hasn't extracted any semantic entities."
          />
        }
      >
        {(data) => (
          <div className="overflow-hidden rounded-lg border border-border bg-card shadow-xs">
            <table className="w-full text-sm">
              <thead className="bg-muted/40">
                <tr className="border-b border-border">
                  <th className="px-3 py-2 text-left text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Name</th>
                  <th className="px-3 py-2 text-left text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Type</th>
                  <th className="px-3 py-2 text-left text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Importance</th>
                  <th className="px-3 py-2 text-left text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Tags</th>
                  <th className="px-3 py-2 text-left text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Source</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/60">
                {data.items.map((e) => (
                  <tr
                    key={e.slug}
                    onClick={() => onSelectSlug(e.slug)}
                    className="cursor-pointer transition-colors duration-150 hover:bg-muted/40"
                  >
                    <td className="px-3 py-2 align-top text-foreground">{e.name}</td>
                    <td className="px-3 py-2 align-top text-xs text-muted-foreground">{e.entity_type}</td>
                    <td className="px-3 py-2 align-top">
                      <ImportanceBadge n={e.importance} />
                    </td>
                    <td className="max-w-xs truncate px-3 py-2 align-top text-xs text-muted-foreground">
                      {e.tags.join(', ') || '—'}
                    </td>
                    <td className="max-w-[16ch] truncate px-3 py-2 align-top">
                      <span className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
                        {e.source}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </QueryState>

      <EntityDetail
        agentId={agentId}
        entity={selected}
        open={selected != null}
        onOpenChange={(o) => !o && onSelectSlug(null)}
        onNavigateEntity={onSelectSlug}
      />
    </div>
  )
}
