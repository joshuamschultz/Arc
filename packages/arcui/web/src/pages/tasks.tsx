import { useMemo, useState } from 'react'
import { Plus } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { FilterPills } from '@/components/filter-pills'
import { StatCard } from '@/components/stat-card'
import { EmptyState, QueryState } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { OperatorModeToggle } from '@/components/operator-mode-toggle'
import { TaskBoard } from '@/components/task-board'
import { fmtSeconds, isBlocked } from '@/lib/tasks'
import { TaskDrawer } from '@/components/task-drawer'
import { CreateTaskSheet } from '@/components/create-task-sheet'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useRoster, useTeamTasks } from '@/lib/queries'
import type { MentionHandle } from '@/components/mention-composer'
import type { Task, TaskPriority, TaskStatus } from '@/lib/types'

const STATUS_FILTERS: (TaskStatus | 'all')[] = [
  'all', 'backlog', 'todo', 'in_progress', 'review', 'done', 'failed',
]
const PRIORITY_FILTERS: (TaskPriority | 'all')[] = ['all', 'low', 'medium', 'high', 'critical']

export function TasksPage() {
  const query = useTeamTasks()
  const roster = useRoster()
  const [operatorMode] = useOperatorMode()
  const [statusFilter, setStatusFilter] = useState<TaskStatus | 'all'>('all')
  const [priorityFilter, setPriorityFilter] = useState<TaskPriority | 'all'>('all')
  const [ownerFilter, setOwnerFilter] = useState('all')
  const [tagFilter, setTagFilter] = useState('all')
  const [selected, setSelected] = useState<Task | null>(null)
  const [creating, setCreating] = useState(false)

  const tasks = useMemo(() => query.data?.tasks ?? [], [query.data])
  const agents = useMemo(() => roster.data?.agents ?? [], [roster.data])

  const statusById = useMemo(() => {
    const m = new Map<string, string>()
    for (const t of tasks) if (t.id) m.set(t.id, t.status ?? 'backlog')
    return m
  }, [tasks])

  const resolveOwner = useMemo(() => {
    const byDid = new Map(agents.filter((a) => a.did).map((a) => [a.did as string, a]))
    return (ownerDid: string | null | undefined): string | null => {
      if (!ownerDid) return null
      const a = byDid.get(ownerDid)
      return a ? String(a.display_name || a.name || ownerDid) : ownerDid
    }
  }, [agents])

  const owners = useMemo(() => {
    const seen = new Map<string, string>()
    for (const t of tasks) {
      if (t.owner_did) seen.set(t.owner_did, resolveOwner(t.owner_did) ?? t.owner_did)
    }
    return [...seen.entries()]
  }, [tasks, resolveOwner])

  const tags = useMemo(() => {
    const seen = new Set<string>()
    for (const t of tasks) for (const tag of t.tags ?? []) seen.add(tag)
    return [...seen].sort()
  }, [tasks])

  const statusCounts = useMemo(() => {
    const c: Record<string, number> = { all: tasks.length }
    for (const t of tasks) c[t.status ?? 'backlog'] = (c[t.status ?? 'backlog'] ?? 0) + 1
    return c
  }, [tasks])

  const priorityCounts = useMemo(() => {
    const c: Record<string, number> = { all: tasks.length }
    for (const t of tasks) c[t.priority ?? 'medium'] = (c[t.priority ?? 'medium'] ?? 0) + 1
    return c
  }, [tasks])

  const metrics = useMemo(() => {
    const today = new Date().toISOString().slice(0, 10)
    let inProgress = 0
    let doneToday = 0
    let failed = 0
    let blocked = 0
    const doneDurations: number[] = [] // seconds, from the real lifecycle fields
    for (const t of tasks) {
      if (t.status === 'in_progress') inProgress++
      if (t.status === 'failed') failed++
      if (isBlocked(t, statusById)) blocked++
      if (t.status === 'done') {
        const completed = t.completed_at ?? t.updated_at
        if (completed?.slice(0, 10) === today) doneToday++
        if (t.duration_seconds != null) doneDurations.push(t.duration_seconds)
        else if (t.started_at && t.completed_at) {
          doneDurations.push((Date.parse(t.completed_at) - Date.parse(t.started_at)) / 1000)
        }
      }
    }
    const avgDone = doneDurations.length
      ? doneDurations.reduce((a, b) => a + b, 0) / doneDurations.length
      : null
    return { inProgress, doneToday, failed, blocked, avgDone }
  }, [tasks, statusById])

  const counts = useMemo(
    () => ({
      tasks: tasks.length,
      inbox: statusCounts.todo ?? 0,
      blocked: metrics.blocked,
      backlog: statusCounts.backlog ?? 0,
    }),
    [tasks.length, statusCounts, metrics.blocked],
  )

  // The board is NOT status-filtered — each column self-populates by its own
  // status, so a task never disappears from the board. Status is applied by
  // focusing a single column instead (see `statusFilter` -> TaskBoard below),
  // which keeps the pill counts (over all tasks) and the columns in agreement.
  const boardTasks = tasks.filter(
    (t) =>
      (priorityFilter === 'all' || t.priority === priorityFilter) &&
      (ownerFilter === 'all' || t.owner_did === ownerFilter) &&
      (tagFilter === 'all' || (t.tags ?? []).includes(tagFilter)),
  )

  const mentionHandles = useMemo<MentionHandle[]>(
    () =>
      agents
        .map((a) => ({
          handle: String(a.name || a.agent_id || ''),
          label: String(a.display_name || a.name || a.agent_id || ''),
          color: typeof a.color === 'string' ? a.color : undefined,
        }))
        .filter((h) => h.handle),
    [agents],
  )

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Tasks"
        description="Fleet-wide task board across all agents."
        actions={
          <>
            <OperatorModeToggle />
            {operatorMode && (
              <Button size="sm" onClick={() => setCreating(true)}>
                <Plus className="size-3.5" /> New task
              </Button>
            )}
          </>
        }
      />
      <div className="flex-1 space-y-4 overflow-hidden p-6">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard label="In progress" value={metrics.inProgress} />
          <StatCard label="Done today" value={metrics.doneToday} />
          <StatCard label="Avg time to done" value={metrics.avgDone != null ? fmtSeconds(metrics.avgDone) : '—'} />
          <StatCard label="Failed" value={metrics.failed} />
        </div>

        <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
          <span className="tabular-nums">{counts.tasks} tasks</span>
          <span className="text-border">·</span>
          <span className="tabular-nums">{counts.inbox} inbox</span>
          <span className="text-border">·</span>
          <span className="tabular-nums">{counts.blocked} blocked</span>
          <span className="text-border">·</span>
          <span className="tabular-nums">{counts.backlog} backlog</span>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <FilterPills
            value={statusFilter}
            onChange={(v) => setStatusFilter(v as TaskStatus | 'all')}
            options={STATUS_FILTERS.map((s) => ({
              value: s,
              label: s === 'all' ? 'All' : s.replace(/_/g, ' '),
              count: statusCounts[s] ?? 0,
            }))}
          />
          <FilterPills
            value={priorityFilter}
            onChange={(v) => setPriorityFilter(v as TaskPriority | 'all')}
            options={PRIORITY_FILTERS.map((p) => ({
              value: p,
              label: p === 'all' ? 'All priority' : p,
              count: priorityCounts[p] ?? 0,
            }))}
          />
          <Select value={ownerFilter} onValueChange={setOwnerFilter}>
            <SelectTrigger size="sm"><SelectValue placeholder="Owner" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All owners</SelectItem>
              {owners.map(([did, label]) => (
                <SelectItem key={did} value={did}>{label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          {tags.length > 0 && (
            <Select value={tagFilter} onValueChange={setTagFilter}>
              <SelectTrigger size="sm"><SelectValue placeholder="Tag" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All tags</SelectItem>
                {tags.map((tag) => (
                  <SelectItem key={tag} value={tag}>{tag}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </div>

        <QueryState
          query={query}
          isEmpty={() => tasks.length === 0}
          empty={<EmptyState title="No tasks across the fleet yet." />}
        >
          {() => (
            <TaskBoard
              tasks={boardTasks}
              resolveOwner={resolveOwner}
              onSelectTask={setSelected}
              focusStatus={statusFilter}
            />
          )}
        </QueryState>
      </div>

      <TaskDrawer
        task={selected}
        open={selected != null}
        onOpenChange={(o) => !o && setSelected(null)}
        operatorMode={operatorMode}
        roster={agents}
        mentionHandles={mentionHandles}
        allTasks={tasks}
      />
      <CreateTaskSheet open={creating} onOpenChange={setCreating} roster={agents} />
    </div>
  )
}
