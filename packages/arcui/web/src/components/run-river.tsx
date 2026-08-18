import { AlignLeft, Pencil, Terminal, GitPullRequest, Wrench, Bot, MessageSquare } from 'lucide-react'
import { mergeTimeline, type Item } from '@/components/run-detail-drawer'
import { StatusChip } from '@/components/ai'
import { SignedSeal } from '@/components/hitl'
import { LoadingRows, EmptyState } from '@/components/states'
import { useRunTimeline } from '@/lib/queries'
import { fmtLatency, fmtNumber, fmtTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { RunSummary } from '@/lib/types'

/**
 * Run River — a run's signed action trace as a vertical timeline. Each tool the
 * agent ran is a governed, signed step; the spine connects them in order. Reads
 * the real run-timeline (same fold as the drawer), rendered as the mockup's
 * signature view.
 */
function toolIcon(name: string) {
  const n = name.toLowerCase()
  if (n.includes('read') || n.includes('cat') || n.includes('grep') || n.includes('ls')) return AlignLeft
  if (n.includes('edit') || n.includes('write') || n.includes('patch')) return Pencil
  if (n.includes('bash') || n.includes('exec') || n.includes('pytest') || n.includes('shell')) return Terminal
  if (n.includes('pr') || n.includes('git') || n.includes('push')) return GitPullRequest
  return Wrench
}

function argOf(input: unknown): string | null {
  if (input == null) return null
  if (typeof input === 'string') return input.length > 80 ? input.slice(0, 80) + '…' : input
  const o = input as Record<string, unknown>
  const v = o.path ?? o.file ?? o.command ?? o.cmd ?? o.query ?? o.repo ?? o.url ?? o.name
  return v != null ? String(v) : null
}

function RiverStep({ item, last }: { item: Item; last: boolean }) {
  let Icon = MessageSquare
  let op = ''
  let arg: string | null = null
  let status: string | undefined
  let latency: number | null | undefined
  let signed = false
  let held = false

  if (item.kind === 'tool') {
    Icon = toolIcon(item.name)
    op = item.name
    arg = argOf(item.input)
    status = item.status
    latency = item.latency_ms
    signed = true
    held = item.status === 'running' || item.status === 'stale'
  } else if (item.kind === 'llm') {
    Icon = Bot
    op = item.model
    arg = item.tokensIn || item.tokensOut ? `${fmtNumber(item.tokensIn)} in / ${fmtNumber(item.tokensOut)} out` : null
    latency = item.latency_ms
  } else {
    Icon = MessageSquare
    op = item.name
  }

  return (
    <div className="relative flex gap-4 pb-5 last:pb-1">
      {!last && <span className="absolute left-[13px] top-7 bottom-0 w-px bg-border" />}
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
        <Icon className="size-3.5" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-semibold text-foreground">{op}</span>
          {arg && <span className="truncate font-mono text-xs text-muted-foreground">{arg}</span>}
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-2.5">
          {status && <StatusChip value={status} />}
          {signed && (
            <span className="inline-flex items-center gap-1 text-[11px] text-signed">
              <SignedSeal className="size-3.5" /> signed
            </span>
          )}
          {latency != null && (
            <span className="text-[11px] tabular-nums text-muted-foreground">{fmtLatency(latency)}</span>
          )}
          {item.ts && <span className="text-[11px] tabular-nums text-muted-foreground/70">{fmtTime(item.ts)}</span>}
        </div>
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
