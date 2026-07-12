// Task-board derived state helpers (shared by the board and the tasks page).

import type { Task } from '@/lib/types'

/** True when any of `task.blocked_by` is not yet `done` (SDD §4 v1-derived state). */
export function isBlocked(task: Task, statusById: Map<string, string>): boolean {
  return (task.blocked_by ?? []).some((depId) => statusById.get(depId) !== 'done')
}
