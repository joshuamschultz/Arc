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
import { useRunTimeline } from '@/lib/queries'
import { fmtLatency, fmtNumber, fmtTime, shortId } from '@/lib/format'
import type { RunSummary, TimelineEntry, Trace } from '@/lib/types'

const CODE_EXEC_TOOLS = new Set(['execute_python', 'execute'])

// A tool call pairs its start (carrying input) with its end/error (carrying
// output). LLM and run markers pass through as their own items.
interface ToolItem {
  kind: 'tool'
  ts?: string | null
  name: string
  isCode: boolean
  input: unknown
  output: unknown
  status: string
  latency_ms?: number | null
  // U13 — when this tool declared a required skill, the loader records which
  // skill it pulled (and whether the load succeeded) on the end event's extra.
  activatedSkill?: string | null
  skillActivated?: boolean
}
interface LlmItem {
  kind: 'llm'
  ts?: string | null
  model: string
  tokensIn: number
  tokensOut: number
  latency_ms?: number | null
  traceId?: string | null // the llm_call record id — deep-links to its ArcLLM call
}
interface RunItem {
  kind: 'run'
  ts?: string | null
  name: string
}
type Item = ToolItem | LlmItem | RunItem

/** Fold raw timeline rows into display items, pairing tool start/end by name. */
function mergeTimeline(entries: TimelineEntry[]): Item[] {
  const items: Item[] = []
  const pending = new Map<string, ToolItem[]>() // tool_name -> open starts (FIFO)

  for (const e of entries) {
    if (e.kind === 'tool_event') {
      const name = e.tool_name ?? '—'
      if (e.phase === 'start') {
        const item: ToolItem = {
          kind: 'tool',
          ts: e.ts,
          name,
          isCode: CODE_EXEC_TOOLS.has(name),
          input: e.extra?.args ?? null,
          output: null,
          status: 'running',
        }
        items.push(item)
        const q = pending.get(name) ?? []
        q.push(item)
        pending.set(name, q)
      } else {
        // end | error — attach output to the earliest open start of this tool.
        const q = pending.get(name)
        const target = q?.shift()
        const out = e.extra?.result ?? null
        const status = e.outcome === 'error' || e.phase === 'error' ? 'error' : 'ok'
        // U13 — the tool->skill activation signal rides the end event's extra.
        const activatedSkill = (e.extra?.activated_skill as string | undefined) ?? null
        const skillActivated = e.extra?.skill_activated as boolean | undefined
        if (target) {
          target.output = out
          target.status = status
          target.latency_ms = e.latency_ms
          target.activatedSkill = activatedSkill
          target.skillActivated = skillActivated
        } else {
          items.push({
            kind: 'tool',
            ts: e.ts,
            name,
            isCode: CODE_EXEC_TOOLS.has(name),
            input: null,
            output: out,
            status,
            latency_ms: e.latency_ms,
            activatedSkill,
            skillActivated,
          })
        }
      }
    } else if (e.kind === 'llm_call') {
      items.push({
        kind: 'llm',
        ts: e.ts,
        model: e.model ?? '—',
        tokensIn: e.prompt_tokens ?? 0,
        tokensOut: e.completion_tokens ?? 0,
        latency_ms: e.latency_ms,
        traceId: e.record_id ?? null,
      })
    } else {
      items.push({ kind: 'run', ts: e.ts, name: e.name ?? 'event' })
    }
  }
  return items
}

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
      <span>{item.name}</span>
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
  const items = data ? mergeTimeline(data.timeline) : []
  // Deep-link: clicking an LLM step opens that exact ArcLLM call's drawer.
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
