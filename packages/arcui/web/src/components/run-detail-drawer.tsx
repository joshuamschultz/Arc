import { useState } from 'react'
import { Wrench, Code, Bot, Circle } from 'lucide-react'
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
import { useRunTimeline } from '@/lib/queries'
import { fmtLatency, fmtNumber, fmtTime, shortId } from '@/lib/format'
import type { RunSummary, TimelineEntry } from '@/lib/types'

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
}
interface LlmItem {
  kind: 'llm'
  ts?: string | null
  model: string
  tokensIn: number
  tokensOut: number
  latency_ms?: number | null
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
        if (target) {
          target.output = out
          target.status = status
          target.latency_ms = e.latency_ms
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
      })
    } else {
      items.push({ kind: 'run', ts: e.ts, name: e.name ?? 'event' })
    }
  }
  return items
}

function ToolRow({ item }: { item: ToolItem }) {
  const [open, setOpen] = useState(false)
  const Icon = item.isCode ? Code : Wrench
  const hasBodies = item.input != null || item.output != null
  return (
    <div className="rounded-lg border border-border/60 bg-muted/10">
      <button
        type="button"
        onClick={() => hasBodies && setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs"
      >
        <span className="w-12 shrink-0 tabular-nums text-muted-foreground">{fmtTime(item.ts)}</span>
        <Icon className="size-3.5 shrink-0 text-primary" />
        <span className="font-mono text-foreground">{item.name}</span>
        <StatusText value={item.status} />
        {item.latency_ms != null && (
          <span className="tabular-nums text-muted-foreground">{fmtLatency(item.latency_ms)}</span>
        )}
        <span className="ml-auto text-[11px] text-muted-foreground">
          {hasBodies ? (open ? 'hide' : 'in / out') : 'no body'}
        </span>
      </button>
      {open && hasBodies && (
        <div className="space-y-2 border-t border-border/60 px-3 py-2">
          <div>
            <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Input</p>
            {item.input != null ? <JsonBlock value={item.input} className="max-h-60" /> : <p className="text-xs text-muted-foreground">—</p>}
          </div>
          <div>
            <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Output</p>
            {item.output != null ? <JsonBlock value={item.output} className="max-h-60" /> : <p className="text-xs text-muted-foreground">—</p>}
          </div>
        </div>
      )}
    </div>
  )
}

function TimelineItem({ item }: { item: Item }) {
  if (item.kind === 'tool') return <ToolRow item={item} />
  if (item.kind === 'llm') {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 text-xs">
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
      </div>
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

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-2xl">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="font-mono text-sm">Run {shortId(run?.run_id ?? '', 18)}</SheetTitle>
          <SheetDescription>
            {run ? `${run.agent} · ${run.turns} turns · ${run.tool_calls} tools · ${run.status}` : ''}
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 space-y-1.5 overflow-auto p-4">
          {isLoading && <LoadingRows rows={6} />}
          {!isLoading && !items.length && <EmptyState title="No steps recorded for this run" />}
          {items.map((item, i) => (
            <TimelineItem key={i} item={item} />
          ))}
        </div>
      </SheetContent>
    </Sheet>
  )
}
