import { Link2, Lock, Eye } from 'lucide-react'
import { SeverityBadge } from '@/components/status-badge'
import { cn } from '@/lib/utils'
import { shortId } from '@/lib/format'
import type { Task } from '@/lib/types'

/** One kanban card — title, priority, owner, blocked badge, run link (FR-11). */
export function TaskCard({
  task,
  ownerLabel,
  blocked,
  onClick,
}: {
  task: Task
  ownerLabel: string | null
  blocked: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'flex w-full flex-col gap-2.5 rounded-lg border border-border bg-card p-3 text-left text-sm shadow-xs transition-colors duration-150',
        'hover:border-primary/35',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60',
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="min-w-0 flex-1 truncate font-medium text-foreground">{task.title}</span>
        <SeverityBadge value={task.priority} />
      </div>
      <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
        <span className="truncate rounded-full bg-muted/50 px-2 py-0.5 text-[11px] text-muted-foreground">
          {ownerLabel ?? 'Unassigned'}
        </span>
        <div className="flex shrink-0 items-center gap-1.5">
          {task.status === 'review' && (
            <span className="flex items-center gap-1 rounded-full bg-status-info/15 px-1.5 py-0.5 text-status-info" title="Awaiting operator review">
              <Eye className="size-3" /> review
            </span>
          )}
          {blocked && (
            <span className="flex items-center gap-1 rounded-full bg-status-warning/15 px-1.5 py-0.5 text-status-warning" title="Blocked on unfinished dependencies">
              <Lock className="size-3" /> blocked
            </span>
          )}
          {task.run_id && (
            <span
              className="flex items-center gap-1 rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground"
              title={`run ${task.run_id}`}
            >
              <Link2 className="size-3" /> {shortId(task.run_id, 8)}
            </span>
          )}
        </div>
      </div>
    </button>
  )
}
