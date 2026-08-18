import { Check, Pause, X } from 'lucide-react'
import { cn } from '@/lib/utils'

/* Circuit-breaker states carry the opposite intuition to their words — a
 * "closed" circuit is the healthy one — so we colour them by meaning and keep
 * the real term as the label. Mirrors the StatusChip look (which has no mapping
 * for breaker states). */
const TONE_CLASS = {
  online: 'bg-status-online/12 text-status-online',
  warning: 'bg-status-warning/12 text-status-warning',
  error: 'bg-status-error/12 text-status-error',
  muted: 'bg-muted text-muted-foreground',
} as const

const BREAKER: Record<
  string,
  { tone: keyof typeof TONE_CLASS; Icon: typeof Check; label: string; hint: string }
> = {
  closed: { tone: 'online', Icon: Check, label: 'Closed', hint: 'healthy' },
  open: { tone: 'error', Icon: X, label: 'Open', hint: 'tripped' },
  half_open: { tone: 'warning', Icon: Pause, label: 'Half-open', hint: 'recovering' },
}

/** A circuit-breaker state as a colour-correct chip with a plain-language hint. */
export function BreakerBadge({ state }: { state?: string }) {
  const key = (state || '').toLowerCase().replace(/[\s-]/g, '_')
  const s = BREAKER[key] ?? {
    tone: 'muted' as const,
    Icon: Pause,
    label: state || 'unknown',
    hint: '',
  }
  const { Icon } = s
  return (
    <span
      title={s.hint}
      className={cn(
        'inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-[11px] font-semibold',
        TONE_CLASS[s.tone],
      )}
    >
      <Icon className="size-3" strokeWidth={2.6} />
      {s.label}
    </span>
  )
}
