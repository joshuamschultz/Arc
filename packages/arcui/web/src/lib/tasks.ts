// Task-board derived state helpers (shared by the board and the tasks page).

import type { Task } from '@/lib/types'

/** True when any of `task.blocked_by` is not yet `done` (SDD §4 v1-derived state). */
export function isBlocked(task: Task, statusById: Map<string, string>): boolean {
  return (task.blocked_by ?? []).some((depId) => statusById.get(depId) !== 'done')
}

/** Subtasks of `parentId` (tasks whose `parent_id` points at it) + done count (P2). */
export function subtaskProgress(
  parentId: string,
  tasks: Task[],
): { children: Task[]; done: number; total: number } {
  const children = tasks.filter((t) => t.parent_id === parentId)
  const done = children.filter((c) => c.status === 'done').length
  return { children, done, total: children.length }
}

/** Human-readable seconds — the lifecycle `duration_seconds` field (P1). */
export function fmtSeconds(seconds: number | null | undefined): string {
  if (seconds == null) return '—'
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  if (seconds < 86_400) return `${(seconds / 3600).toFixed(1)}h`
  return `${(seconds / 86_400).toFixed(1)}d`
}
