import { useMemo } from 'react'
import { TaskCard } from '@/components/task-card'
import type { Task, TaskStatus } from '@/lib/types'

const COLUMNS: { id: TaskStatus; label: string }[] = [
  { id: 'backlog', label: 'Backlog' },
  { id: 'todo', label: 'To Do' },
  { id: 'in_progress', label: 'In Progress' },
  { id: 'review', label: 'Review' },
  { id: 'done', label: 'Done' },
  { id: 'failed', label: 'Failed' },
]

/** True when any of `task.blocked_by` is not yet `done` (SDD §4 v1-derived state). */
export function isBlocked(task: Task, statusById: Map<string, string>): boolean {
  return (task.blocked_by ?? []).some((depId) => statusById.get(depId) !== 'done')
}

/** Kanban board — one column per `TaskStatus`, plus a `failed` lane. */
export function TaskBoard({
  tasks,
  resolveOwner,
  onSelectTask,
}: {
  tasks: Task[]
  resolveOwner: (ownerDid: string | null | undefined) => string | null
  onSelectTask: (task: Task) => void
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

  return (
    <div className="flex h-full gap-3 overflow-x-auto pb-2">
      {COLUMNS.map((col) => {
        const items = byColumn.get(col.id) ?? []
        return (
          <div key={col.id} className="flex w-72 shrink-0 flex-col gap-2 rounded-xl border border-border bg-card/30 p-2.5">
            <div className="flex items-center justify-between px-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              <span>{col.label}</span>
              <span className="tabular-nums">{items.length}</span>
            </div>
            <div className="flex flex-1 flex-col gap-2 overflow-y-auto">
              {items.length === 0 ? (
                <div className="rounded-lg border border-dashed border-border/60 p-4 text-center text-xs text-muted-foreground">
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
