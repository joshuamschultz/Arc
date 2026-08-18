import { Link2, Lock } from 'lucide-react'
import { SeverityBadge } from '@/components/status-badge'
import { StatusChip } from '@/components/ai'
import { cn } from '@/lib/utils'
import { shortId } from '@/lib/format'
import type { Task } from '@/lib/types'

/** One kanban card — title, priority, owner, status, blocked badge, run link. */
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

      <div className="flex items-center justify-between gap-2">
        <span className="truncate rounded-full bg-muted/50 px-2 py-0.5 text-[11px] text-muted-foreground">
          {ownerLabel ?? 'Unassigned'}
        </span>
        <div className="flex shrink-0 items-center gap-1.5">
          {blocked && (
            <span
              className="flex items-center gap-1 rounded-md bg-status-warning/15 px-1.5 py-0.5 text-[11px] font-semibold text-status-warning"
              title="Blocked on unfinished dependencies"
            >
              <Lock className="size-3" /> blocked
            </span>
          )}
          <StatusChip value={task.status} />
        </div>
      </div>

      {task.run_id && (
        <div className="flex items-center gap-1.5 border-t border-border/60 pt-2 font-mono text-[11px] text-muted-foreground">
          <Link2 className="size-3 shrink-0" />
          {shortId(task.run_id, 8)}
        </div>
      )}
    </button>
  )
}
