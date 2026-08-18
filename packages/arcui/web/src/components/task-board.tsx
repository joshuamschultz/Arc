import { useMemo } from 'react'
import { TaskCard } from '@/components/task-card'
import { isBlocked } from '@/lib/tasks'
import { cn } from '@/lib/utils'
import type { Task, TaskStatus } from '@/lib/types'

// A calm per-column tone — a single status dot beside the label, never a fat
// left-bar. Blue marks the active `in_progress` lane (design: blue = running).
const COLUMNS: { id: TaskStatus; label: string; dot: string }[] = [
  { id: 'backlog', label: 'Backlog', dot: 'bg-status-idle' },
  { id: 'todo', label: 'To Do', dot: 'bg-status-info' },
  { id: 'in_progress', label: 'In Progress', dot: 'bg-primary' },
  { id: 'review', label: 'Review', dot: 'bg-status-info' },
  { id: 'done', label: 'Done', dot: 'bg-status-online' },
  { id: 'failed', label: 'Failed', dot: 'bg-status-error' },
]

/** Kanban board — one column per `TaskStatus`, plus a `failed` lane.
 *
 * Columns always self-populate by each task's own status, so a task never
 * vanishes from the board just because a status filter is active — the caller
 * feeds the owner/priority/tag-filtered list and lets the columns sort by
 * status. When `focusStatus` names a single status, only that column renders
 * (full-width focus) so selecting a status pill shows exactly its tasks; `all`
 * renders every column. All six columns flex to share width and hit a min so
 * `done`/`failed` stay reachable (the row scrolls only when they can't fit). */
export function TaskBoard({
  tasks,
  resolveOwner,
  onSelectTask,
  focusStatus = 'all',
}: {
  tasks: Task[]
  resolveOwner: (ownerDid: string | null | undefined) => string | null
  onSelectTask: (task: Task) => void
  focusStatus?: TaskStatus | 'all'
}) {
  const statusById = useMemo(() => {
    const m = new Map<string, string>()
    for (const t of tasks) if (t.id) m.set(t.id, t.status ?? 'backlog')
    return m
  }, [tasks])

  const byColumn = useMemo(() => {
    const grouped = new Map<TaskStatus, Task[]>(COLUMNS.map((c) => [c.id, []]))
    for (const t of tasks) {
      const col = grouped.get((t.status ?? 'backlog') as TaskStatus)
      if (col) col.push(t)
    }
    return grouped
  }, [tasks])

  const columns = focusStatus === 'all' ? COLUMNS : COLUMNS.filter((c) => c.id === focusStatus)

  return (
    <div className="flex h-full gap-3 overflow-x-auto pb-2">
      {columns.map((col) => {
        const items = byColumn.get(col.id) ?? []
        return (
          <div
            key={col.id}
            className="flex min-w-[11rem] flex-1 basis-0 flex-col gap-2 rounded-lg border border-border/60 bg-muted/20 p-2.5"
          >
            <div className="flex items-center justify-between px-1 py-0.5">
              <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                <span className={cn('size-1.5 rounded-full', col.dot)} />
                {col.label}
              </span>
              <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium tabular-nums text-muted-foreground">
                {items.length}
              </span>
            </div>
            <div className="flex flex-1 flex-col gap-2 overflow-y-auto">
              {items.length === 0 ? (
                <div className="rounded-md border border-dashed border-border/60 bg-background/30 p-4 text-center text-[11px] text-muted-foreground/70">
                  Empty
                </div>
              ) : (
                items.map((t) => (
                  <TaskCard
                    key={t.id}
                    task={t}
                    ownerLabel={resolveOwner(t.owner_did)}
                    blocked={isBlocked(t, statusById)}
                    onClick={() => onSelectTask(t)}
                  />
                ))
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
