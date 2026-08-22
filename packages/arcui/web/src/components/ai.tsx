import { useState, type ReactNode } from 'react'
import {
  ChevronDown,
  Sparkles,
  Wrench,
  Check,
  X,
  Pause,
  Eye,
  Loader,
  Gauge,
  TrendingUp,
  TrendingDown,
} from 'lucide-react'
import { cn } from '@/lib/utils'

/* ---------------------------------------------------------------------------
 * AI-native components (Beautiful-UI vocabulary) — the reusable building blocks
 * for agent activity across the app. Token-driven, flat, distinctive. See
 * REDESIGN.md §6. Pair with components/hitl.tsx for approval / trifecta / seal.
 * ------------------------------------------------------------------------- */

/** Collapsible reasoning trace — "Thought for 3s · N steps". */
export function ThinkingTrace({
  summary,
  steps,
  defaultOpen = false,
}: {
  summary: string
  steps: ReactNode[]
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-muted/30">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-medium text-muted-foreground hover:text-foreground"
      >
        <Sparkles className="size-3.5 text-signed" />
        {summary}
        <ChevronDown
          className={cn('ml-auto size-3.5 transition-transform', open && 'rotate-180')}
        />
      </button>
      {open && (
        <div className="flex flex-col gap-1.5 px-3 pb-3 pl-8">
          {steps.map((s, i) => (
            <div key={i} className="relative text-xs leading-relaxed text-muted-foreground">
              <span className="absolute -left-3 top-1.5 size-1 rounded-full bg-signed" />
              {s}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/** A tool call as a compact chip. */
export function ToolChip({
  tool,
  arg,
  ok = true,
}: {
  tool: string
  arg?: ReactNode
  ok?: boolean
}) {
  return (
    <span className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1 text-[11px] text-muted-foreground">
      <Wrench className="size-3 shrink-0 text-muted-foreground/70" />
      <span className="font-medium text-foreground/80">{tool}</span>
      {arg != null && <span className="truncate font-mono text-muted-foreground/70">{arg}</span>}
      {ok && <Check className="size-3 shrink-0 text-status-online" strokeWidth={3} />}
    </span>
  )
}

// Static class strings — Tailwind JIT cannot see dynamically built names.
const TONE_CLASS: Record<string, string> = {
  online: 'bg-status-online/12 text-status-online',
  info: 'bg-status-info/12 text-status-info',
  warning: 'bg-status-warning/12 text-status-warning',
  error: 'bg-status-error/12 text-status-error',
  muted: 'bg-muted text-muted-foreground',
}
const STATUS: Record<string, { tone: keyof typeof TONE_CLASS; Icon: typeof Check; spin?: boolean }> = {
  ok: { tone: 'online', Icon: Check },
  success: { tone: 'online', Icon: Check },
  done: { tone: 'online', Icon: Check },
  completed: { tone: 'online', Icon: Check },
  online: { tone: 'online', Icon: Check },
  running: { tone: 'info', Icon: Loader, spin: true },
  in_progress: { tone: 'info', Icon: Loader, spin: true },
  review: { tone: 'info', Icon: Eye },
  pending: { tone: 'warning', Icon: Pause },
  held: { tone: 'warning', Icon: Pause },
  stale: { tone: 'warning', Icon: Pause },
  waiting: { tone: 'warning', Icon: Pause },
  // Hit a turn/cost/token cap after real work — amber, not the red of a crash.
  limited: { tone: 'warning', Icon: Gauge },
  error: { tone: 'error', Icon: X },
  failed: { tone: 'error', Icon: X },
  denied: { tone: 'error', Icon: X },
  deny: { tone: 'error', Icon: X },
}

/** A status as a plain-language chip with an icon + tone. */
export function StatusChip({ value }: { value?: string }) {
  const key = (value || '').toLowerCase()
  const s = STATUS[key] ?? { tone: 'muted' as const, Icon: Pause }
  const { Icon } = s
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-[11px] font-semibold capitalize',
        TONE_CLASS[s.tone],
      )}
    >
      <Icon className={cn('size-3', s.spin && 'animate-spin')} strokeWidth={2.6} />
      {(value || 'unknown').replace(/_/g, ' ')}
    </span>
  )
}

/** Streaming answer with a blinking caret while it fills. */
export function StreamingText({ text, streaming }: { text: string; streaming?: boolean }) {
  return (
    <span className="text-sm leading-relaxed text-foreground">
      {text}
      {streaming && (
        <span className="ml-0.5 inline-block h-4 w-[2px] translate-y-0.5 animate-pulse bg-primary" />
      )}
    </span>
  )
}

/** A richer stat / insight tile with an optional trend and sparkline slot. */
export function InsightStat({
  label,
  value,
  delta,
  spark,
  icon,
}: {
  label: string
  value: ReactNode
  delta?: number
  spark?: ReactNode
  icon?: ReactNode
}) {
  const up = (delta ?? 0) >= 0
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-4 transition-colors hover:border-foreground/15">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          {label}
        </span>
        {icon && <span className="text-muted-foreground/70">{icon}</span>}
      </div>
      <div className="flex items-end justify-between gap-2">
        <span className="font-display text-[26px] font-extrabold leading-none tabular-nums tracking-tight text-foreground">
          {value}
        </span>
        {delta != null && (
          <span
            className={cn(
              'inline-flex items-center gap-0.5 text-[11px] font-semibold tabular-nums',
              up ? 'text-status-online' : 'text-status-error',
            )}
          >
            {up ? <TrendingUp className="size-3" /> : <TrendingDown className="size-3" />}
            {Math.abs(delta)}%
          </span>
        )}
      </div>
      {spark && <div className="h-8">{spark}</div>}
    </div>
  )
}
