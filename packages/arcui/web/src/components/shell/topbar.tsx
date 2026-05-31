import { ArcLogo } from '@/components/arc-logo'
import { useConnectionStore, type ConnectionStatus } from '@/store/connection'
import { cn } from '@/lib/utils'

const DOT: Record<ConnectionStatus, string> = {
  connected: 'bg-status-online',
  connecting: 'bg-status-idle animate-pulse',
  reconnecting: 'bg-status-warning animate-pulse',
  disconnected: 'bg-status-error',
}

const LABEL: Record<ConnectionStatus, string> = {
  connected: 'Live',
  connecting: 'Connecting',
  reconnecting: 'Reconnecting',
  disconnected: 'Offline',
}

export function Topbar() {
  const status = useConnectionStore((s) => s.status)
  return (
    <header className="flex h-12 items-center gap-3 border-b border-border bg-background px-4">
      <div className="flex items-center gap-2">
        <ArcLogo />
        <span className="text-[15px] font-bold tracking-wide text-foreground">
          ARC
        </span>
        <span className="text-xs text-muted-foreground">Agent Platform</span>
      </div>

      <div className="flex-1" />

      <div className="flex items-center gap-2 rounded-full border border-border bg-card px-3 py-1 text-xs font-medium text-muted-foreground">
        <span className={cn('size-2 rounded-full', DOT[status])} />
        {LABEL[status]}
      </div>
    </header>
  )
}
