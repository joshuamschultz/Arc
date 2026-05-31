import { cn } from '@/lib/utils'

/** Online/offline/degraded dot + label for agents. */
export function StatusDot({
  online,
  degraded,
  className,
}: {
  online?: boolean
  degraded?: boolean
  className?: string
}) {
  const color = !online
    ? 'bg-status-idle'
    : degraded
      ? 'bg-status-warning'
      : 'bg-status-online'
  return (
    <span className={cn('inline-flex items-center gap-1.5', className)}>
      <span className={cn('size-2 rounded-full', color, online && !degraded && 'animate-pulse')} />
      <span className="text-xs text-muted-foreground">
        {!online ? 'Offline' : degraded ? 'Degraded' : 'Online'}
      </span>
    </span>
  )
}

const SEVERITY: Record<string, string> = {
  critical: 'bg-severity-critical/15 text-severity-critical border-severity-critical/30',
  high: 'bg-severity-high/15 text-severity-high border-severity-high/30',
  medium: 'bg-severity-medium/15 text-severity-medium border-severity-medium/30',
  low: 'bg-severity-low/15 text-severity-low border-severity-low/30',
}

/** Colored pill for a severity / status string. */
export function SeverityBadge({ value }: { value?: string }) {
  const key = (value || '').toLowerCase()
  const cls = SEVERITY[key] ?? 'bg-muted text-muted-foreground border-border'
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-md border px-1.5 py-0.5 text-[11px] font-medium capitalize',
        cls,
      )}
    >
      {value || 'unknown'}
    </span>
  )
}

const STATUS_TONE: Record<string, string> = {
  ok: 'text-status-online',
  success: 'text-status-online',
  done: 'text-status-online',
  completed: 'text-status-online',
  error: 'text-status-error',
  failed: 'text-status-error',
  denied: 'text-status-error',
  deny: 'text-status-error',
  pending: 'text-status-warning',
  in_progress: 'text-status-info',
  running: 'text-status-info',
}

/** Inline status text with a tone dot. */
export function StatusText({ value }: { value?: string }) {
  const key = (value || '').toLowerCase()
  const tone = STATUS_TONE[key] ?? 'text-muted-foreground'
  return (
    <span className={cn('inline-flex items-center gap-1.5 text-xs font-medium', tone)}>
      <span className="size-1.5 rounded-full bg-current" />
      <span className="capitalize">{(value || 'unknown').replace(/_/g, ' ')}</span>
    </span>
  )
}
