import { useConnectionStore, type ConnectionStatus } from '@/store/connection'
import { cn } from '@/lib/utils'

const BANNER: Record<
  Exclude<ConnectionStatus, 'connected'>,
  { label: string; className: string }
> = {
  connecting: {
    label: 'Connecting to the Arc backend…',
    className: 'bg-muted text-muted-foreground',
  },
  reconnecting: {
    label: 'Connection lost — reconnecting…',
    className: 'bg-status-warning/15 text-status-warning',
  },
  disconnected: {
    label: 'Disconnected from the Arc backend.',
    className: 'bg-destructive/15 text-destructive',
  },
}

/** Thin status strip; renders nothing while the socket is healthy. */
export function ConnectionBanner() {
  const status = useConnectionStore((s) => s.status)
  if (status === 'connected') return null
  const cfg = BANNER[status]
  return (
    <div
      className={cn(
        'flex items-center justify-center gap-2 px-4 py-1.5 text-xs font-medium',
        cfg.className,
      )}
      role="status"
    >
      <span className="size-1.5 animate-pulse rounded-full bg-current" />
      {cfg.label}
    </div>
  )
}
