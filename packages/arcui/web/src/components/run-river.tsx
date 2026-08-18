import { useState } from 'react'
import {
  AlignLeft,
  Pencil,
  Terminal,
  GitPullRequest,
  GitBranch,
  Wrench,
  Bot,
  MessageSquare,
  ChevronRight,
  PanelRight,
  Sparkles,
} from 'lucide-react'
import { mergeTimeline, describeAction, type Item } from '@/lib/run-timeline'
import { StatusChip } from '@/components/ai'
import { SignedSeal } from '@/components/hitl'
import { LoadingRows, EmptyState } from '@/components/states'
import { JsonBlock } from '@/components/json-block'
import { LlmContent } from '@/components/llm-content-renderer'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { useRunTimeline, useTraceDetail } from '@/lib/queries'
import { fmtCost, fmtLatency, fmtNumber, fmtTime, shortId } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { RunSummary, Trace } from '@/lib/types'

/**
 * Run River — a run's signed action trace as a vertical timeline. Each tool the
 * agent ran is a governed, signed step; the spine connects them in order. Every
 * step expands to reveal what it actually did — a tool's input and output, or a
 * model call's full request and response — so a viewer can read the run, not
 * just watch it scroll.
 */
function StepIcon({ item }: { item: Item }) {
  const c = 'size-3.5'
  if (item.kind === 'llm') return <Bot className={c} />
  if (item.kind === 'run') return <MessageSquare className={c} />
  if (item.kind === 'spawn') return <GitBranch className={c} />
  const n = item.name.toLowerCase()
  if (n.includes('read') || n.includes('cat') || n.includes('grep') || n.includes('ls'))
    return <AlignLeft className={c} />
  if (n.includes('edit') || n.includes('write') || n.includes('patch')) return <Pencil className={c} />
  if (n.includes('bash') || n.includes('exec') || n.includes('pytest') || n.includes('shell'))
    return <Terminal className={c} />
  if (n.includes('pr') || n.includes('git') || n.includes('push')) return <GitPullRequest className={c} />
  return <Wrench className={c} />
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n) + '…' : s
}

// The arg keys worth surfacing on the step row, in the order a reader scans for
// them. A concise summary shows up to two so a `bash`/`edit` call reads at a
// glance without opening it.
const SUMMARY_KEYS = ['path', 'file', 'command', 'cmd', 'query', 'pattern', 'url', 'repo', 'name']

/** A one-line human summary of a tool call's key args, for the collapsed row. */
function toolSummary(input: unknown): string | null {
  if (input == null) return null
  if (typeof input === 'string') return truncate(input, 100)
  if (typeof input !== 'object') return String(input)
  const o = input as Record<string, unknown>
  const vals: string[] = []
  for (const k of SUMMARY_KEYS) {
    const v = o[k]
    if (v != null && (typeof v === 'string' || typeof v === 'number')) {
      vals.push(truncate(String(v), 70))
      if (vals.length >= 2) break
    }
  }
  return vals.length > 0 ? vals.join('  ·  ') : null
}

/** Pretty, wrapping payload for a tool's Input/Output — JSON pretty-printed and
 *  wrapped so long lines never overflow; a string body renders as text, not a
 *  quoted JSON blob. Height-capped with its own vertical scroll. */
function PayloadBlock({ value }: { value: unknown }) {
  let text: string
  try {
    text = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  } catch {
    text = String(value)
  }
  return (
    <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-muted/30 p-3 font-mono text-xs leading-relaxed text-foreground">
      {text}
    </pre>
  )
}

/** A small uppercase section label above a detail body. */
function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground/70">
      {children}
    </div>
  )
}

/** Small labelled key/value shown in a step's detail grid. */
function Meta({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground/70">
        {label}
      </span>
      <span className="font-mono text-xs text-foreground">{children}</span>
    </div>
  )
}

interface Msg {
  role?: string
  content?: unknown
}

/** The request messages off a fetched trace envelope, when present. */
function reqMessages(trace: Trace | undefined): Msg[] {
  const req = trace?.request as Record<string, unknown> | undefined
  const msgs = req?.messages
  return Array.isArray(msgs) ? (msgs as Msg[]) : []
}

/** Renderable content out of a provider response envelope (Anthropic `content[]`,
 *  OpenAI `choices[].message.content`). Returns undefined for unknown shapes so
 *  the caller falls back to raw JSON. */
function respContent(response: unknown): unknown {
  if (response == null || typeof response !== 'object') return undefined
  const obj = response as Record<string, unknown>
  if (Array.isArray(obj.content) || typeof obj.content === 'string') return obj.content
  const choices = obj.choices
  if (Array.isArray(choices) && choices.length > 0) {
    const msg = (choices[0] as Record<string, unknown>)?.message as
      | Record<string, unknown>
      | undefined
    if (msg && (typeof msg.content === 'string' || Array.isArray(msg.content))) return msg.content
  }
  return undefined
}

/**
 * Right-side drawer for a model call. Opens straight to the formatted
 * "Structured" view — header facts from the timeline item, request messages and
 * the response rendered through {@link LlmContent}. A Structured/Raw toggle lets
 * a reader drop to the raw payload; Structured is the default so the drawer is
 * useful the instant it opens.
 */
function LlmDrawer({
  item,
  open,
  onOpenChange,
}: {
  item: Extract<Item, { kind: 'llm' }>
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { data, isLoading } = useTraceDetail(open ? (item.traceId ?? null) : null)
  const [view, setView] = useState<'structured' | 'raw'>('structured')
  const messages = reqMessages(data)
  const response = data?.response
  const rc = respContent(response)

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-2xl"
      >
        <SheetHeader className="border-b border-border px-5 py-4">
          <div className="flex items-center gap-2 pr-8">
            <Bot className="size-4 text-muted-foreground" />
            <SheetTitle className="text-sm">{item.model}</SheetTitle>
          </div>
          <SheetDescription>Model call — the request and the response</SheetDescription>
        </SheetHeader>

        <div className="border-b border-border px-5 py-2">
          <div className="inline-flex gap-1 rounded-lg border border-border bg-card p-1">
            {(['structured', 'raw'] as const).map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => setView(v)}
                className={cn(
                  'rounded-md px-3 py-1 text-xs font-semibold transition-colors',
                  view === v
                    ? 'bg-primary/12 text-foreground'
                    : 'text-muted-foreground hover:text-foreground',
                )}
              >
                {v === 'structured' ? 'Structured' : 'Raw'}
              </button>
            ))}
          </div>
        </div>

        <div className="flex-1 overflow-auto p-5">
          <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Meta label="Model">{item.model}</Meta>
            <Meta label="Tokens">
              {fmtNumber(item.tokensIn)} in / {fmtNumber(item.tokensOut)} out
            </Meta>
            {item.costUsd != null && <Meta label="Cost">{fmtCost(item.costUsd)}</Meta>}
            {item.requestId && <Meta label="Call">{shortId(item.requestId, 16)}</Meta>}
          </div>

          {view === 'raw' ? (
            <JsonBlock value={data ?? item} />
          ) : isLoading ? (
            <LoadingRows rows={6} />
          ) : messages.length === 0 && response === undefined ? (
            <p className="text-xs text-muted-foreground">
              No stored request/response for this call.
            </p>
          ) : (
            <div className="space-y-4">
              {messages.length > 0 && (
                <div className="space-y-2">
                  <SectionLabel>Request</SectionLabel>
                  {messages.map((m, i) => (
                    <div
                      key={i}
                      className="rounded-lg border border-l-2 border-border border-l-primary/40 bg-muted/20 p-3"
                    >
                      <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.08em] text-primary">
                        {m.role || 'message'}
                      </div>
                      <LlmContent content={m.content} />
                    </div>
                  ))}
                </div>
              )}
              {response !== undefined && (
                <div className="space-y-2">
                  <SectionLabel>Response</SectionLabel>
                  {rc !== undefined ? <LlmContent content={rc} /> : <JsonBlock value={response} />}
                </div>
              )}
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}

/** Input and output bodies for a tool call (present when raw capture is on). */
function ToolDetail({ item }: { item: Extract<Item, { kind: 'tool' }> }) {
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {item.status && <Meta label="Status">{item.status}</Meta>}
        {item.latency_ms != null && <Meta label="Latency">{fmtLatency(item.latency_ms)}</Meta>}
        {item.activatedSkill && <Meta label="Skill">{item.activatedSkill}</Meta>}
        {item.requestId && <Meta label="Call">{item.requestId}</Meta>}
      </div>
      <div className="space-y-1">
        <SectionLabel>Input</SectionLabel>
        {item.input != null ? (
          <PayloadBlock value={item.input} />
        ) : (
          <p className="text-xs text-muted-foreground">Not captured.</p>
        )}
      </div>
      <div className="space-y-1">
        <SectionLabel>Output</SectionLabel>
        {item.output != null ? (
          <PayloadBlock value={item.output} />
        ) : (
          <p className="text-xs text-muted-foreground">Not captured.</p>
        )}
      </div>
    </div>
  )
}

function RiverStep({ item, last }: { item: Item; last: boolean }) {
  // Tool steps expand inline; LLM steps open the right-side drawer.
  const [open, setOpen] = useState(false)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const { title, description } = describeAction(item)
  const isTool = item.kind === 'tool'
  const isLlm = item.kind === 'llm'
  const summary = isTool
    ? toolSummary(item.input)
    : isLlm && (item.tokensIn || item.tokensOut)
      ? `${fmtNumber(item.tokensIn)} in / ${fmtNumber(item.tokensOut)} out`
      : null
  const status = isTool ? item.status : undefined
  const held = isTool && (item.status === 'running' || item.status === 'stale')
  const latency = isTool || isLlm ? item.latency_ms : undefined
  const clickable = isTool || isLlm

  return (
    <div className="relative flex gap-4 pb-5 last:pb-1">
      {!last && <span className="absolute bottom-0 left-[13px] top-7 w-px bg-border" />}
      <span
        className={cn(
          'z-[1] grid size-7 shrink-0 place-items-center rounded-lg border',
          held
            ? 'border-status-warning/50 bg-status-warning/10 text-status-warning'
            : item.kind === 'spawn'
              ? 'border-primary/30 bg-primary/8 text-primary'
              : item.kind === 'run'
                ? 'border-transparent bg-primary/12 text-primary'
                : 'border-border bg-card text-muted-foreground',
        )}
      >
        <StepIcon item={item} />
      </span>
      <div className="min-w-0 flex-1">
        <button
          type="button"
          disabled={!clickable}
          onClick={() => (isLlm ? setDrawerOpen(true) : setOpen((o) => !o))}
          className={cn(
            'group -mx-2 flex w-[calc(100%+1rem)] items-start gap-2 rounded-lg px-2 py-1 text-left',
            clickable && 'transition-colors hover:bg-muted/40',
          )}
        >
          {isTool && (
            <ChevronRight
              className={cn(
                'mt-0.5 size-3.5 shrink-0 text-muted-foreground transition-transform',
                open && 'rotate-90',
              )}
            />
          )}
          <span className={cn('min-w-0 flex-1', !isTool && 'pl-[22px]')}>
            <span className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-semibold text-foreground">{title}</span>
              {summary && (
                <span className="truncate font-mono text-xs text-muted-foreground">{summary}</span>
              )}
              {isTool && item.skillActivated && (
                <span className="inline-flex items-center gap-1 rounded border border-primary/30 bg-primary/8 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                  <Sparkles className="size-3" /> skill
                </span>
              )}
            </span>
            <span className="mt-0.5 block text-xs text-muted-foreground">{description}</span>
            <span className="mt-1.5 flex flex-wrap items-center gap-2.5">
              {status && <StatusChip value={status} />}
              {isTool && (
                <span className="inline-flex items-center gap-1 text-[11px] text-signed">
                  <SignedSeal className="size-3.5" /> signed
                </span>
              )}
              {latency != null && (
                <span className="text-[11px] tabular-nums text-muted-foreground">{fmtLatency(latency)}</span>
              )}
              {item.ts && <span className="text-[11px] tabular-nums text-muted-foreground/70">{fmtTime(item.ts)}</span>}
            </span>
          </span>
          {isLlm && (
            <span className="ml-auto inline-flex shrink-0 items-center gap-1 self-center text-[11px] text-muted-foreground transition-colors group-hover:text-primary">
              <PanelRight className="size-3.5" /> open
            </span>
          )}
        </button>
        {open && isTool && (
          <div className="ml-[22px] mt-2 rounded-lg border border-border bg-muted/20 p-3">
            <ToolDetail item={item} />
          </div>
        )}
        {isLlm && <LlmDrawer item={item} open={drawerOpen} onOpenChange={setDrawerOpen} />}
      </div>
    </div>
  )
}

export function RunRiver({ run }: { run: RunSummary | null }) {
  const { data, isLoading } = useRunTimeline(run?.run_id ?? null)

  if (!run) {
    return (
      <div className="grid h-full place-items-center p-8 text-center">
        <div className="max-w-xs text-sm text-muted-foreground">
          Select a run to see its signed action trace — every tool the agent ran, in order.
        </div>
      </div>
    )
  }

  const items = data ? mergeTimeline(data.timeline, run.status === 'running') : []
  const skillsUsed = Array.from(
    new Set(
      items
        .filter((i): i is Extract<Item, { kind: 'tool' }> => i.kind === 'tool')
        .map((i) => i.activatedSkill)
        .filter((s): s is string => !!s),
    ),
  )

  return (
    <div className="p-6">
      <div className="mb-5">
        <h2 className="font-display text-lg font-bold tracking-tight text-foreground">
          {run.agent} · run {run.run_id.length > 10 ? run.run_id.slice(0, 8) : run.run_id}
        </h2>
        <div className="mt-1 flex items-center gap-2.5 text-sm text-muted-foreground">
          <StatusChip value={run.status} />
          <span>{run.turns} turns</span>
          <span>·</span>
          <span>{run.tool_calls} tools</span>
        </div>
        {skillsUsed.length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <span className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground/70">
              Skills used
            </span>
            {skillsUsed.map((s) => (
              <span
                key={s}
                className="inline-flex items-center gap-1 rounded border border-primary/30 bg-primary/8 px-1.5 py-0.5 text-[11px] font-medium text-primary"
              >
                <Sparkles className="size-3" /> {s}
              </span>
            ))}
          </div>
        )}
      </div>
      {isLoading ? (
        <LoadingRows rows={5} />
      ) : items.length === 0 ? (
        <EmptyState title="No steps recorded for this run" />
      ) : (
        <div className="max-w-2xl">
          {items.map((item, i) => (
            <RiverStep key={i} item={item} last={i === items.length - 1} />
          ))}
        </div>
      )}
    </div>
  )
}
