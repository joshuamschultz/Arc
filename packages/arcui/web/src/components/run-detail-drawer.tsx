import { useState } from 'react'
import { Wrench, Code, Bot, Circle, Sparkles } from 'lucide-react'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { JsonBlock } from '@/components/json-block'
import { LoadingRows, EmptyState } from '@/components/states'
import { StatusText } from '@/components/status-badge'
import { TraceDrawer } from '@/components/trace-drawer'
import { mergeTimeline, type Item, type ToolItem } from '@/lib/run-timeline'
import { useRunTimeline } from '@/lib/queries'
import { fmtLatency, fmtNumber, fmtTime, shortId } from '@/lib/format'
import type { RunSummary, Trace } from '@/lib/types'

/** Render a tool payload readably: strings as text, objects as formatted JSON. */
function PayloadView({ value }: { value: unknown }) {
  if (value == null) return <p className="text-xs text-muted-foreground">—</p>
  if (typeof value === 'string') {
    return (
      <pre className="max-h-60 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted/40 p-2.5 font-mono text-xs text-foreground">
        {value}
      </pre>
    )
  }
  return <JsonBlock value={value} className="max-h-60" />
}

function ToolRow({ item }: { item: ToolItem }) {
  const [open, setOpen] = useState(false)
  const [view, setView] = useState<'structured' | 'raw'>('structured')
  const Icon = item.isCode ? Code : Wrench
  const hasBodies = item.input != null || item.output != null
  return (
    <div className="rounded-lg border border-border/60 bg-muted/10 transition-colors hover:border-border">
      <button
        type="button"
        onClick={() => hasBodies && setOpen((o) => !o)}
        className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
      >
        <span className="w-12 shrink-0 tabular-nums text-muted-foreground">{fmtTime(item.ts)}</span>
        <Icon className="size-3.5 shrink-0 text-primary" />
        <span className="font-mono text-foreground">{item.name}</span>
        {item.implicit && (
          <span
            className="rounded border border-border/60 px-1 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground"
            title="ran automatically, not a model-issued tool call"
          >
            auto
          </span>
        )}
        <StatusText value={item.status} />
        {item.activatedSkill && (
          <span
            className={
              item.skillActivated === false
                ? 'inline-flex items-center gap-1 rounded border border-status-warning/40 bg-status-warning/10 px-1.5 py-0.5 text-[11px] text-status-warning'
                : 'inline-flex items-center gap-1 rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-[11px] text-primary'
            }
            title={
              item.skillActivated === false
                ? `required skill "${item.activatedSkill}" could not be loaded`
                : `pulled required skill "${item.activatedSkill}"`
            }
          >
            <Sparkles className="size-3" />
            {item.skillActivated === false ? `skill missing: ${item.activatedSkill}` : `pulled skill: ${item.activatedSkill}`}
          </span>
        )}
        {item.latency_ms != null && (
          <span className="tabular-nums text-muted-foreground">{fmtLatency(item.latency_ms)}</span>
        )}
        <span className="ml-auto text-[11px] text-muted-foreground">
          {hasBodies ? (open ? 'hide' : 'in / out') : 'no body'}
        </span>
      </button>
      {open && hasBodies && (
        <div className="space-y-2 border-t border-border/60 px-3 py-2">
          <div className="flex gap-1 text-[11px]">
            {(['structured', 'raw'] as const).map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => setView(v)}
                className={`rounded-md px-2 py-0.5 capitalize transition-colors ${
                  view === v ? 'bg-primary/12 text-foreground ring-1 ring-primary/20' : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                {v}
              </button>
            ))}
          </div>
          {view === 'structured' ? (
            <>
              <div>
                <p className="mb-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Input (sent to tool)</p>
                <PayloadView value={item.input} />
              </div>
              <div>
                <p className="mb-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Output (returned)</p>
                <PayloadView value={item.output} />
              </div>
            </>
          ) : (
            <JsonBlock value={{ input: item.input, output: item.output }} className="max-h-72" />
          )}
        </div>
      )}
    </div>
  )
}

function TimelineItem({ item, onOpenTrace }: { item: Item; onOpenTrace: (traceId: string) => void }) {
  if (item.kind === 'tool') return <ToolRow item={item} />
  if (item.kind === 'llm') {
    const traceId = item.traceId
    const inner = (
      <>
        <span className="w-12 shrink-0 tabular-nums text-muted-foreground">{fmtTime(item.ts)}</span>
        <Bot className="size-3.5 shrink-0 text-chart-2" />
        <span className="font-mono text-foreground">{item.model}</span>
        {(item.tokensIn > 0 || item.tokensOut > 0) && (
          <span className="tabular-nums text-muted-foreground">
            {fmtNumber(item.tokensIn)} in / {fmtNumber(item.tokensOut)} out tok
          </span>
        )}
        {item.latency_ms != null && (
          <span className="tabular-nums text-muted-foreground">{fmtLatency(item.latency_ms)}</span>
        )}
        {traceId && <span className="ml-auto text-[11px] text-primary">view call →</span>}
      </>
    )
    return traceId ? (
      <button
        type="button"
        onClick={() => onOpenTrace(traceId)}
        className="flex w-full items-center gap-2 rounded px-3 py-1.5 text-left text-xs hover:bg-muted/40"
      >
        {inner}
      </button>
    ) : (
      <div className="flex items-center gap-2 px-3 py-1.5 text-xs">{inner}</div>
    )
  }
  return (
    <div className="flex items-center gap-2 px-3 py-1.5 text-xs text-muted-foreground">
      <span className="w-12 shrink-0 tabular-nums">{fmtTime(item.ts)}</span>
      <Circle className="size-2.5 shrink-0" />
      <span>
        {item.kind === 'spawn'
          ? `Spawned sub-agent ${(item.childDid.split('/').pop() ?? '').slice(0, 8)}`
          : item.name}
      </span>
    </div>
  )
}

/** Per-run timeline in a side drawer: tools (with in/out), code, llm, lifecycle. */
export function RunDetailDrawer({
  run,
  open,
  onOpenChange,
}: {
  run: RunSummary | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { data, isLoading } = useRunTimeline(open ? run?.run_id ?? null : null)
  const items = data ? mergeTimeline(data.timeline, run?.status === 'running') : []
  const [selectedTrace, setSelectedTrace] = useState<Trace | null>(null)

  return (
    <>
      <Sheet open={open} onOpenChange={onOpenChange}>
        <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-2xl">
          <SheetHeader className="border-b border-border px-5 py-4">
            <div className="flex items-center gap-2 pr-8">
              <SheetTitle className="text-sm font-semibold text-foreground">Run</SheetTitle>
              <span className="inline-flex items-center rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-foreground">
                {shortId(run?.run_id ?? '', 18)}
              </span>
              {run && <StatusText value={run.status} />}
            </div>
            <SheetDescription>
              {run ? `${run.agent} · ${run.turns} turns · ${run.tool_calls} tools · ${run.status}` : ''}
            </SheetDescription>
          </SheetHeader>
          <div className="flex-1 space-y-1.5 overflow-auto p-4">
            {isLoading && <LoadingRows rows={6} />}
            {!isLoading && !items.length && <EmptyState title="No steps recorded for this run" />}
            {items.map((item, i) => (
              <TimelineItem key={i} item={item} onOpenTrace={(id) => setSelectedTrace({ trace_id: id })} />
            ))}
          </div>
        </SheetContent>
      </Sheet>
      <TraceDrawer
        trace={selectedTrace}
        open={selectedTrace !== null}
        onOpenChange={(o) => !o && setSelectedTrace(null)}
      />
    </>
  )
}
