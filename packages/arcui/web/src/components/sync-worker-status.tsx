import { useQuery } from '@tanstack/react-query'
import { CircleCheck, RefreshCw, TriangleAlert } from 'lucide-react'
import { apiGet } from '@/lib/api'
import { cn } from '@/lib/utils'

/** The deployment's sync worker: the one process that writes connected stores. */
export interface SyncWorkerStatus {
  state: 'up' | 'restarting' | 'down'
  pid: number | null
  restarts: number
  detail: string
  retry_in_seconds?: number | null
}

export const useSyncWorkerStatus = () =>
  useQuery<SyncWorkerStatus>({
    queryKey: ['knowledge', 'sync-worker'],
    queryFn: ({ signal }) => apiGet('/api/knowledge/sync-worker', signal),
    refetchInterval: 5_000,
  })

const COPY: Record<SyncWorkerStatus['state'], { title: string; body: string }> = {
  up: { title: 'Sync running', body: 'Connected sources sync in their own process.' },
  restarting: {
    title: 'Sync paused: restarting',
    body: 'Syncs resume where they stopped. Search keeps working.',
  },
  down: {
    title: 'Sync stopped',
    body: 'No source is syncing. Search keeps working on what is already indexed.',
  },
}

/** One line on the Sources tab: is the sync worker up, restarting or down? */
export function SyncWorkerStatusLine() {
  const { data } = useSyncWorkerStatus()
  const state = data?.state
  if (state !== 'up' && state !== 'restarting' && state !== 'down') return null
  const copy = COPY[state]
  const Icon = state === 'up' ? CircleCheck : state === 'restarting' ? RefreshCw : TriangleAlert
  return (
    <div
      role="status"
      aria-label="Sync worker"
      data-state={state}
      className={cn(
        'mb-3 flex items-center gap-2 rounded-lg border px-3 py-2 text-xs',
        state === 'up' && 'border-border bg-muted/20 text-muted-foreground',
        state === 'restarting' && 'border-amber-500/40 bg-amber-500/10 text-foreground',
        state === 'down' && 'border-destructive/40 bg-destructive/10 text-foreground',
      )}
    >
      <Icon className={cn('size-3.5 shrink-0', state === 'restarting' && 'animate-spin')} />
      <span className="font-medium">{copy.title}</span>
      <span className="text-muted-foreground">{copy.body}</span>
      {state !== 'up' && data?.detail ? (
        <span className="ml-auto font-mono text-[10px] text-muted-foreground">{data.detail}</span>
      ) : null}
    </div>
  )
}
