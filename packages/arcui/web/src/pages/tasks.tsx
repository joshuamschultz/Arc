import { useMemo, useState } from 'react'
import { Plus, Loader, CheckCircle2, Timer, XCircle } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { FieldHelp } from '@/components/help'
import { FilterPills } from '@/components/filter-pills'
import { InsightStat } from '@/components/ai'
import { EmptyState, ErrorState, LoadingRows } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { OperatorModeToggle } from '@/components/operator-mode-toggle'
import { TaskBoard } from '@/components/task-board'
import { fmtSeconds } from '@/lib/tasks'
import { TaskDrawer } from '@/components/task-drawer'
import { CreateTaskSheet } from '@/components/create-task-sheet'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useRoster, useTeamTaskBoard } from '@/lib/queries'
import type { MentionHandle } from '@/components/mention-composer'
import type { Task, TaskPriority, TaskStatus } from '@/lib/types'

const STATUS_FILTERS: (TaskStatus | 'all')[] = [
  'all', 'backlog', 'todo', 'in_progress', 'review', 'done', 'failed',
]
const PRIORITY_FILTERS: (TaskPriority | 'all')[] = ['all', 'low', 'medium', 'high', 'critical']

export function TasksPage() {
  const [pageCursors, setPageCursors] = useState<string[]>([])
  const cursor = pageCursors.at(-1) ?? null
  const roster = useRoster()
  const [operatorMode] = useOperatorMode()
  const [statusFilter, setStatusFilter] = useState<TaskStatus | 'all'>('all')
  const [priorityFilter, setPriorityFilter] = useState<TaskPriority | 'all'>('all')
  const [ownerFilter, setOwnerFilter] = useState('all')
  const [tagFilter, setTagFilter] = useState('all')
  const [ownerSearch, setOwnerSearch] = useState('')
  const [tagSearch, setTagSearch] = useState('')
  const filters = useMemo(() => ({
    status: statusFilter === 'all' ? undefined : statusFilter,
    priority: priorityFilter === 'all' ? undefined : priorityFilter,
    owner_did: ownerFilter === 'all' ? undefined : ownerFilter,
    tag: tagFilter === 'all' ? undefined : tagFilter,
  }), [statusFilter, priorityFilter, ownerFilter, tagFilter])
  const query = useTeamTaskBoard(cursor, filters)
  const nextCursor = query.data?.next_cursor
  const [selected, setSelected] = useState<Task | null>(null)
  const [creating, setCreating] = useState(false)
  const [creationNotice, setCreationNotice] = useState<string | null>(null)

  const tasks = useMemo(() => query.data?.tasks ?? [], [query.data])
  const facets = query.data?.facets
  const projections = query.data?.projections ?? {}
  const agents = useMemo(() => roster.data?.agents ?? [], [roster.data])

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
    for (const did of Object.keys(facets?.owners ?? {})) seen.set(did, resolveOwner(did) ?? did)
    return [...seen.entries()]
  }, [facets?.owners, resolveOwner])

  const tags = useMemo(() => {
    return Object.keys(facets?.tags ?? {}).sort()
  }, [facets?.tags])

  const statusCounts: Record<string, number> = { all: facets?.total ?? 0, ...facets?.statuses }
  const priorityCounts: Record<string, number> = { all: facets?.total ?? 0, ...facets?.priorities }
  const counts = { inbox: statusCounts.todo ?? 0, blocked: facets?.blocked ?? 0,
    backlog: statusCounts.backlog ?? 0 }

  // The server applies all filters before paging. The board focuses the
  // selected status column while the pills retain store-wide facet counts.
  const boardTasks = tasks

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
        {creationNotice && <div role="status" className="rounded-md border border-border p-2 text-xs">{creationNotice}</div>}
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <InsightStat
            label="In progress"
            value={statusCounts.in_progress ?? 0}
            icon={<Loader className="size-3.5" />}
          />
          <InsightStat
            label="Done today"
            value={facets?.done_today ?? 0}
            icon={<CheckCircle2 className="size-3.5" />}
          />
          <InsightStat
            label="Avg time to done"
            value={facets?.avg_done_seconds != null ? fmtSeconds(facets.avg_done_seconds) : '—'}
            icon={<Timer className="size-3.5" />}
          />
          <InsightStat
            label="Failed"
            value={statusCounts.failed ?? 0}
            icon={<XCircle className="size-3.5" />}
          />
        </div>

        <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
          <span className="tabular-nums">{tasks.length} shown · {facets?.total ?? 0} total{nextCursor ? ' · more available' : ''}</span>
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
            onChange={(v) => { setPageCursors([]); setStatusFilter(v as TaskStatus | 'all') }}
            options={STATUS_FILTERS.map((s) => ({
              value: s,
              label: s === 'all' ? 'All' : s.replace(/_/g, ' '),
              count: statusCounts[s] ?? 0,
            }))}
          />
          <FilterPills
            value={priorityFilter}
            onChange={(v) => { setPageCursors([]); setPriorityFilter(v as TaskPriority | 'all') }}
            options={PRIORITY_FILTERS.map((p) => ({
              value: p,
              label: p === 'all' ? 'All priority' : p,
              count: priorityCounts[p] ?? 0,
            }))}
          />
          <Select value={ownerFilter} onValueChange={(v) => { setPageCursors([]); setOwnerFilter(v) }}>
            <SelectTrigger size="sm"><SelectValue placeholder="Owner" /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All owners</SelectItem>
              {owners.map(([did, label]) => (
                <SelectItem key={did} value={did}>{label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <FieldHelp helpKey="tasks.owner_filter" route="tasks" />
          {facets?.owners_truncated && <Input aria-label="Owner DID filter" placeholder="Filter by owner DID"
            value={ownerSearch} onChange={(e) => setOwnerSearch(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { setPageCursors([]); setOwnerFilter(ownerSearch || 'all') } }} />}
          {tags.length > 0 && (
            <><Select value={tagFilter} onValueChange={(v) => { setPageCursors([]); setTagFilter(v) }}>
              <SelectTrigger size="sm"><SelectValue placeholder="Tag" /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All tags</SelectItem>
                {tags.map((tag) => (
                  <SelectItem key={tag} value={tag}>{tag}</SelectItem>
                ))}
              </SelectContent>
            </Select><FieldHelp helpKey="tasks.tag_filter" route="tasks" /></>
          )}
          {facets?.tags_truncated && <Input aria-label="Tag filter" placeholder="Filter by tag"
            value={tagSearch} onChange={(e) => setTagSearch(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { setPageCursors([]); setTagFilter(tagSearch || 'all') } }} />}
        </div>

        {query.isPending ? <LoadingRows /> : query.isError ? <ErrorState error={query.error} /> :
          tasks.length === 0 ? <EmptyState title={facets?.total ? 'No tasks match these filters.' : 'No tasks across the fleet yet.'} /> : (
            <TaskBoard
              tasks={boardTasks}
              resolveOwner={resolveOwner}
              onSelectTask={setSelected}
              focusStatus={statusFilter}
              projections={projections}
            />
          )}
        {pageCursors.length > 0 && (
          <Button variant="outline" size="sm" onClick={() => setPageCursors((current) => current.slice(0, -1))}>
            Previous page
          </Button>
        )}
        {nextCursor && (
          <Button
            variant="outline"
            size="sm"
            disabled={query.isFetching}
            onClick={() => setPageCursors((current) => [...current, nextCursor])}
          >
            Load more tasks
          </Button>
        )}
      </div>

      <TaskDrawer
        task={selected}
        open={selected != null}
        onOpenChange={(o) => !o && setSelected(null)}
        operatorMode={operatorMode}
        roster={agents}
        mentionHandles={mentionHandles}
        allTasks={tasks}
        projection={selected ? projections[selected.id] : undefined}
      />
      <CreateTaskSheet open={creating} onOpenChange={setCreating} roster={agents}
        onCreated={(task) => setCreationNotice(task.owner_notification === 'not_applicable'
          ? 'Task created in the fleet backlog.'
          : task.owner_notification === 'sent'
            ? 'Task created and owner notified.'
            : `Task created; owner notification ${task.owner_notification ?? 'unknown'}. Check the task before trying again.`)} />
    </div>
  )
}
