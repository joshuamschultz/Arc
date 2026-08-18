import { useState } from 'react'
import {
  AlignLeft,
  Pencil,
  Terminal,
  GitPullRequest,
  Wrench,
  Bot,
  MessageSquare,
  ChevronRight,
  Sparkles,
} from 'lucide-react'
import { mergeTimeline, describeAction, type Item } from '@/lib/run-timeline'
import { StatusChip } from '@/components/ai'
import { SignedSeal } from '@/components/hitl'
import { LoadingRows, EmptyState } from '@/components/states'
import { JsonBlock } from '@/components/json-block'
import { LlmContent } from '@/components/llm-content-renderer'
import { useRunTimeline, useTraceDetail } from '@/lib/queries'
import { fmtLatency, fmtNumber, fmtTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { RunSummary } from '@/lib/types'

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
  const n = item.name.toLowerCase()
  if (n.includes('read') || n.includes('cat') || n.includes('grep') || n.includes('ls'))
    return <AlignLeft className={c} />
  if (n.includes('edit') || n.includes('write') || n.includes('patch')) return <Pencil className={c} />
  if (n.includes('bash') || n.includes('exec') || n.includes('pytest') || n.includes('shell'))
    return <Terminal className={c} />
  if (n.includes('pr') || n.includes('git') || n.includes('push')) return <GitPullRequest className={c} />
  return <Wrench className={c} />
}

function argOf(input: unknown): string | null {
  if (input == null) return null
  if (typeof input === 'string') return input.length > 80 ? input.slice(0, 80) + '…' : input
  const o = input as Record<string, unknown>
  const v = o.path ?? o.file ?? o.command ?? o.cmd ?? o.query ?? o.repo ?? o.url ?? o.name
  return v != null ? String(v) : null
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

/** Full request/response for a model call, fetched on expand by its trace id. */
function LlmDetail({ item }: { item: Extract<Item, { kind: 'llm' }> }) {
  const { data, isLoading } = useTraceDetail(item.traceId ?? null)
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Meta label="Model">{item.model}</Meta>
        <Meta label="Tokens">
          {fmtNumber(item.tokensIn)} in / {fmtNumber(item.tokensOut)} out
        </Meta>
        {item.costUsd != null && <Meta label="Cost">${item.costUsd.toFixed(4)}</Meta>}
        {item.requestId && <Meta label="Call">{item.requestId}</Meta>}
      </div>
      {isLoading ? (
        <LoadingRows rows={2} />
      ) : data ? (
        <div className="space-y-3">
          {data.request != null && (
            <div>
              <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground/70">
                Request
              </div>
              <LlmContent content={data.request} />
            </div>
          )}
          {data.response != null && (
            <div>
              <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground/70">
                Response
              </div>
              <LlmContent content={data.response} />
            </div>
          )}
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">No stored request/response for this call.</p>
      )}
    </div>
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
      <div>
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground/70">
          Input
        </div>
        {item.input != null ? (
          <JsonBlock value={item.input} />
        ) : (
          <p className="text-xs text-muted-foreground">Not captured.</p>
        )}
      </div>
      <div>
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground/70">
          Output
        </div>
        {item.output != null ? (
          <JsonBlock value={item.output} />
        ) : (
          <p className="text-xs text-muted-foreground">Not captured.</p>
        )}
      </div>
    </div>
  )
}

function RiverStep({ item, last }: { item: Item; last: boolean }) {
  const [open, setOpen] = useState(false)
  const { title, description } = describeAction(item)
  const arg = item.kind === 'tool' ? argOf(item.input) : item.kind === 'llm' && (item.tokensIn || item.tokensOut) ? `${fmtNumber(item.tokensIn)} in / ${fmtNumber(item.tokensOut)} out` : null
  const status = item.kind === 'tool' ? item.status : undefined
  const held = item.kind === 'tool' && (item.status === 'running' || item.status === 'stale')
  const latency = item.kind !== 'run' ? item.latency_ms : undefined
  const expandable = item.kind === 'tool' || item.kind === 'llm'

  return (
    <div className="relative flex gap-4 pb-5 last:pb-1">
      {!last && <span className="absolute bottom-0 left-[13px] top-7 w-px bg-border" />}
      <span
        className={cn(
          'z-[1] grid size-7 shrink-0 place-items-center rounded-lg border',
          held
            ? 'border-status-warning/50 bg-status-warning/10 text-status-warning'
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
          disabled={!expandable}
          onClick={() => setOpen((o) => !o)}
          className={cn(
            'group -mx-2 flex w-[calc(100%+1rem)] items-start gap-2 rounded-lg px-2 py-1 text-left',
            expandable && 'transition-colors hover:bg-muted/40',
          )}
        >
          {expandable && (
            <ChevronRight
              className={cn(
                'mt-0.5 size-3.5 shrink-0 text-muted-foreground transition-transform',
                open && 'rotate-90',
              )}
            />
          )}
          <span className={cn('min-w-0 flex-1', !expandable && 'pl-[22px]')}>
            <span className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-semibold text-foreground">{title}</span>
              {arg && <span className="truncate font-mono text-xs text-muted-foreground">{arg}</span>}
              {item.kind === 'tool' && item.skillActivated && (
                <span className="inline-flex items-center gap-1 rounded border border-primary/30 bg-primary/8 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                  <Sparkles className="size-3" /> skill
                </span>
              )}
            </span>
            <span className="mt-0.5 block text-xs text-muted-foreground">{description}</span>
            <span className="mt-1.5 flex flex-wrap items-center gap-2.5">
              {status && <StatusChip value={status} />}
              {item.kind === 'tool' && (
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
        </button>
        {open && expandable && (
          <div className="ml-[22px] mt-2 rounded-lg border border-border bg-muted/20 p-3">
            {item.kind === 'llm' ? <LlmDetail item={item} /> : <ToolDetail item={item as Extract<Item, { kind: 'tool' }>} />}
          </div>
        )}
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
