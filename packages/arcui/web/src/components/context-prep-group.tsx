import { useState } from 'react'
import { ChevronRight } from 'lucide-react'
import { StatusChip } from '@/components/ai'
import type { ContextPrepItem, RetrievalCandidate } from '@/lib/run-timeline'
import { fmtLatency, fmtNumber, fmtTime } from '@/lib/format'
import { cn } from '@/lib/utils'

// Everything the agent gathered before its first model turn. Collapsed it is one
// summary line; expanded it lists the ordered sub-steps. Snippets arrive redacted
// from the server and are rendered strictly as text nodes — never as HTML.

function SubStep({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <li className="space-y-1">
      <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground/70">{label}</div>
      <div className="text-xs text-foreground">{children}</div>
    </li>
  )
}

function Candidate({ item }: { item: RetrievalCandidate }) {
  return (
    <li className="rounded-md border border-border/60 bg-muted/20 p-2">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={cn(
            'rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
            item.included ? 'bg-primary/12 text-primary' : 'bg-muted text-muted-foreground',
          )}
        >
          {item.included ? 'used' : 'excluded'}
        </span>
        <span className="font-medium text-foreground">{item.title}</span>
        <span className="font-mono text-[11px] text-muted-foreground">{item.path ?? item.source}</span>
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 text-[11px] text-muted-foreground">
        <span>
          {item.source_kind}: {item.source}
        </span>
        <span className="tabular-nums">score {item.score.toFixed(2)}</span>
        <span>{item.classification}</span>
        <span>{item.reason}</span>
      </div>
      {item.snippet && (
        <p className="mt-1 whitespace-pre-wrap break-words font-mono text-[11px] text-muted-foreground">{item.snippet}</p>
      )}
    </li>
  )
}

function retrievalLine(item: ContextPrepItem): string | null {
  const r = item.retrieval
  if (!r) return null
  const used = r.items.filter((i) => i.included).length
  return `retrieval ${r.status} · ${used}/${r.items.length} items · ${fmtNumber(r.tokens_injected)} tok`
}

function totalLatency(item: ContextPrepItem): number {
  return (item.strategy?.latency_ms ?? 0) + (item.retrieval?.latency_ms ?? 0)
}

export function ContextPrepGroup({ item }: { item: ContextPrepItem }) {
  const [open, setOpen] = useState(false)
  const summary = [
    item.strategy ? `strategy ${item.strategy.strategy}` : null,
    retrievalLine(item),
    fmtLatency(totalLatency(item)),
  ].filter(Boolean)
  const r = item.retrieval
  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="group -mx-2 flex w-[calc(100%+1rem)] items-start gap-2 rounded-lg px-2 py-1 text-left transition-colors hover:bg-muted/40"
      >
        <ChevronRight
          className={cn('mt-0.5 size-3.5 shrink-0 text-muted-foreground transition-transform', open && 'rotate-90')}
        />
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-semibold text-foreground">Context prep</span>
            <span className="text-xs text-muted-foreground">{summary.join(' · ')}</span>
          </span>
          <span className="mt-0.5 block text-xs text-muted-foreground">
            What the agent gathered before its first model turn
          </span>
          {item.ts && (
            <span className="mt-1 block text-[11px] tabular-nums text-muted-foreground/70">{fmtTime(item.ts)}</span>
          )}
        </span>
      </button>
      {open && (
        <ol className="ml-[22px] mt-2 space-y-3 rounded-lg border border-border bg-muted/20 p-3">
          {item.strategy && (
            <SubStep label="Strategy selected">
              {item.strategy.strategy} — {item.strategy.reason}
              <span className="ml-2 tabular-nums text-muted-foreground">{fmtLatency(item.strategy.latency_ms)}</span>
            </SubStep>
          )}
          {item.system && (
            <SubStep label="System context">
              cached: {item.system.cached ? 'yes' : 'no'} · {fmtNumber(item.system.tokens)} tok
            </SubStep>
          )}
          {r && (
            <SubStep label="Retrieval">
              <div className="mb-1 flex flex-wrap items-center gap-2">
                <StatusChip value={r.status} />
                <span className="font-mono text-muted-foreground">{r.query}</span>
                <span className="tabular-nums text-muted-foreground">
                  {fmtLatency(r.latency_ms)} of {fmtLatency(r.budget_ms)} budget
                </span>
              </div>
              {r.reason && <p className="mb-1 text-muted-foreground">{r.reason}</p>}
              <ul className="space-y-1.5">
                {r.items.map((c, i) => (
                  <Candidate key={`${c.source}-${i}`} item={c} />
                ))}
              </ul>
            </SubStep>
          )}
          {item.session && (
            <SubStep label="Session">
              {fmtNumber(item.session.turns)} turns · {fmtNumber(item.session.tokens)} tok
            </SubStep>
          )}
          {item.skipped.map((s) => (
            <SubStep key={s.step} label="Skipped">
              skipped: {s.step} timed out
              {s.reason && <span className="ml-2 text-muted-foreground">{s.reason}</span>}
            </SubStep>
          ))}
        </ol>
      )}
    </div>
  )
}
