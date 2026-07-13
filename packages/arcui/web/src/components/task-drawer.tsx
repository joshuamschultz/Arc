import { useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { JsonBlock } from '@/components/json-block'
import { StatusText, SeverityBadge } from '@/components/status-badge'
import { MentionComposer, type MentionHandle } from '@/components/mention-composer'
import { useTeamStream } from '@/hooks/use-team-stream'
import { useTaskActivity } from '@/lib/queries'
import { apiDelete, apiPatch, apiPost, ApiError } from '@/lib/api'
import { relativeTime } from '@/lib/format'
import { fmtSeconds, subtaskProgress } from '@/lib/tasks'
import type { Agent, Task, TaskPriority } from '@/lib/types'

const PRIORITIES: TaskPriority[] = ['low', 'medium', 'high', 'critical']

// Every open card + operator post lands here — a single well-known channel,
// auto-created on first post (arcui.messaging.build_team_post_forwarder).
// Addressing is done by @mention (SPEC-055 wakes the mentioned owner), not by
// a per-owner channel, so no DM plumbing is needed.
const STEER_CHANNEL = 'tasks'

function Field({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">{label}</div>
      <div className="truncate font-mono text-[13px] text-foreground">{value ?? '—'}</div>
    </div>
  )
}

/** Task detail drawer (SPEC-056 Phase D) — mirrors `ScheduleDrawer`'s shape.
 *  At-rest (`status !== 'in_progress'`): operator can edit title/description/
 *  priority/owner via `PATCH /api/tasks/{id}`. In-flight: edit is blocked
 *  server-side (409 `task_in_progress`, NFR-4) — the footer swaps to a
 *  "steer owner" composer that @mentions the owner over the team channel
 *  instead (SDD §6). */
export function TaskDrawer({
  task,
  open,
  onOpenChange,
  operatorMode,
  roster,
  mentionHandles,
  allTasks = [],
}: {
  task: Task | null
  open: boolean
  onOpenChange: (o: boolean) => void
  operatorMode: boolean
  roster: Agent[]
  mentionHandles: MentionHandle[]
  allTasks?: Task[]
}) {
  const queryClient = useQueryClient()
  const activity = useTaskActivity(task?.id ?? null)
  const atRest = task?.status !== 'in_progress'
  const { status: steerStatus, post: steerPost } = useTeamStream(!atRest ? STEER_CHANNEL : null)

  const [editing, setEditing] = useState(false)
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [priority, setPriority] = useState<TaskPriority>('medium')
  const [ownerDid, setOwnerDid] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [steerText, setSteerText] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [deciding, setDeciding] = useState(false)

  const ownerAgent = roster.find((a) => a.did === task?.owner_did)

  // Reset edit/action state whenever a different task (or none) is shown —
  // state adjusted during render, keyed on the task id.
  const taskKey = task?.id ?? null
  const [prevTaskKey, setPrevTaskKey] = useState<string | null>(null)
  if (taskKey !== prevTaskKey) {
    setPrevTaskKey(taskKey)
    if (task != null) {
      setTitle(task.title)
      setDescription(task.description ?? '')
      setPriority((task.priority as TaskPriority) ?? 'medium')
      setOwnerDid(task.owner_did ?? '')
      setEditing(false)
      setError(null)
      setConfirmDelete(false)
      setDeleting(false)
      setStopping(false)
      setDeciding(false)
      setSteerText(ownerAgent?.name ? `@${ownerAgent.name} ` : '')
    }
  }

  if (task == null) return null

  const ownerLabel = ownerAgent
    ? String(ownerAgent.display_name || ownerAgent.name || task.owner_did)
    : task.owner_did || 'Unassigned'

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      await apiPatch(`/api/tasks/${encodeURIComponent(task.id)}`, {
        title,
        description,
        priority,
        owner_did: ownerDid || null,
      })
      await queryClient.invalidateQueries({
        predicate: (q) => q.queryKey.some((k) => k === 'tasks'),
      })
      setEditing(false)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to save task')
    } finally {
      setSaving(false)
    }
  }

  const remove = async () => {
    setDeleting(true)
    setError(null)
    try {
      await apiDelete(`/api/tasks/${encodeURIComponent(task.id)}`)
      await queryClient.invalidateQueries({
        predicate: (q) => q.queryKey.some((k) => k === 'tasks'),
      })
      onOpenChange(false)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to delete task')
      setDeleting(false)
    }
  }

  const stop = async () => {
    setStopping(true)
    setError(null)
    try {
      await apiPost(`/api/tasks/${encodeURIComponent(task.id)}/cancel`)
      await queryClient.invalidateQueries({
        predicate: (q) => q.queryKey.some((k) => k === 'tasks'),
      })
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to stop task')
    } finally {
      setStopping(false)
    }
  }

  const decide = async (approve: boolean) => {
    setDeciding(true)
    setError(null)
    try {
      await apiPost(`/api/tasks/${encodeURIComponent(task.id)}/${approve ? 'approve' : 'reject'}`)
      await queryClient.invalidateQueries({
        predicate: (q) => q.queryKey.some((k) => k === 'tasks'),
      })
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to record decision')
    } finally {
      setDeciding(false)
    }
  }

  const sendSteer = () => {
    if (!steerText.trim()) return
    steerPost(steerText)
    setSteerText(ownerAgent?.name ? `@${ownerAgent.name} ` : '')
  }

  const subtasks = subtaskProgress(task.id, allTasks)
  const deps = (task.blocked_by ?? []).map((id) => allTasks.find((t) => t.id === id) ?? { id })

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-xl">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="truncate text-sm">{task.title}</SheetTitle>
          <SheetDescription className="flex flex-wrap items-center gap-2">
            <span className="rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
              {task.id}
            </span>
            <StatusText value={task.status} />
            <SeverityBadge value={task.priority} />
            {!atRest && <span className="text-muted-foreground">edit-at-rest only — steer below</span>}
          </SheetDescription>
        </SheetHeader>

        <div className="flex-1 space-y-5 overflow-auto p-5">
          {editing ? (
            <section className="space-y-3">
              <div className="space-y-1.5">
                <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Title</label>
                <Input value={title} onChange={(e) => setTitle(e.target.value)} />
              </div>
              <div className="space-y-1.5">
                <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Description</label>
                <textarea
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  rows={3}
                  className="w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-xs outline-none transition-[color,box-shadow] hover:border-muted-foreground/40 focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/60"
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Priority</label>
                  <Select value={priority} onValueChange={(v) => setPriority(v as TaskPriority)}>
                    <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {PRIORITIES.map((p) => (
                        <SelectItem key={p} value={p}>{p}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Owner</label>
                  <Select
                    value={ownerDid || '__none__'}
                    onValueChange={(v) => setOwnerDid(v === '__none__' ? '' : v)}
                  >
                    <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="__none__">Unassigned</SelectItem>
                      {roster.filter((a) => a.did).map((a) => (
                        <SelectItem key={a.did} value={a.did as string}>
                          {String(a.display_name || a.name || a.did)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
              {error && <p className="text-xs text-destructive">{error}</p>}
              <div className="flex gap-2">
                <Button size="sm" disabled={saving || !title.trim()} onClick={save}>
                  {saving ? 'Saving…' : 'Save'}
                </Button>
                <Button size="sm" variant="outline" disabled={saving} onClick={() => setEditing(false)}>
                  Cancel
                </Button>
              </div>
            </section>
          ) : (
            <section className="grid grid-cols-2 gap-x-4 gap-y-3">
              <Field label="Owner" value={ownerLabel} />
              <Field label="Creator" value={task.creator_did} />
              <Field label="Parent" value={task.parent_id} />
              <Field
                label="Run"
                value={
                  task.run_id ? (
                    <Link
                      to={`/arcrun?run=${encodeURIComponent(task.run_id)}`}
                      className="text-primary hover:underline"
                      title="Open in ArcRun"
                    >
                      {task.run_id}
                    </Link>
                  ) : (
                    '—'
                  )
                }
              />
              <Field label="Attempts" value={`${task.attempts ?? 0} / ${task.max_attempts ?? 3}`} />
              <Field label="Duration" value={fmtSeconds(task.duration_seconds)} />
              <Field label="Created" value={task.created_at ? relativeTime(task.created_at) : '—'} />
              <Field label="Started" value={task.started_at ? relativeTime(task.started_at) : '—'} />
              <Field label="Completed" value={task.completed_at ? relativeTime(task.completed_at) : '—'} />
              <Field label="Tags" value={task.tags?.length ? task.tags.join(', ') : '—'} />
            </section>
          )}

          {!editing && subtasks.total > 0 && (
            <section className="space-y-2">
              <h3 className="flex items-center justify-between text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                <span>Subtasks</span>
                <span className="tabular-nums text-foreground">{subtasks.done}/{subtasks.total} done</span>
              </h3>
              <ul className="space-y-1">
                {subtasks.children.map((c) => (
                  <li key={c.id} className="flex items-center justify-between gap-2 rounded-md border border-border bg-card px-2.5 py-1.5 text-xs">
                    <span className="truncate text-foreground">{c.title}</span>
                    <StatusText value={c.status} />
                  </li>
                ))}
              </ul>
            </section>
          )}

          {!editing && deps.length > 0 && (
            <section className="space-y-2">
              <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Blocked by</h3>
              <ul className="space-y-1">
                {deps.map((d) => (
                  <li key={d.id} className="flex items-center justify-between gap-2 rounded-md border border-border bg-card px-2.5 py-1.5 text-xs">
                    <span className="truncate text-foreground">{('title' in d && d.title) || d.id}</span>
                    <StatusText value={'status' in d ? d.status : undefined} />
                  </li>
                ))}
              </ul>
            </section>
          )}

          {task.last_error && !editing && (
            <section className="space-y-2">
              <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Last error</h3>
              <p className="whitespace-pre-wrap rounded-lg border border-status-error/30 bg-status-error/10 p-3 text-xs text-status-error">
                {task.last_error}
              </p>
            </section>
          )}

          {task.description && !editing && (
            <section className="space-y-2">
              <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Description</h3>
              <p className="whitespace-pre-wrap rounded-lg border border-border bg-muted/20 p-3 text-sm text-foreground">
                {task.description}
              </p>
            </section>
          )}

          {task.status === 'done' && task.output != null && (
            <section className="space-y-2">
              <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Output</h3>
              <JsonBlock value={task.output} />
            </section>
          )}

          {task.resolution && (
            <section className="space-y-2">
              <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Resolution</h3>
              <p className="text-sm text-foreground">{task.resolution}</p>
            </section>
          )}

          <section className="space-y-2">
            <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Activity</h3>
            {activity.isLoading ? (
              <p className="text-xs text-muted-foreground">Loading…</p>
            ) : (activity.data?.events ?? []).length === 0 ? (
              <p className="text-xs text-muted-foreground">No recorded activity.</p>
            ) : (
              <ul className="space-y-1.5">
                {(activity.data?.events ?? []).map((e, i) => (
                  <li
                    key={i}
                    className="flex items-center justify-between gap-2 rounded-lg border border-border bg-card px-3 py-2 text-xs transition-colors duration-150 hover:bg-muted/40"
                  >
                    <span className="font-medium text-foreground">{String(e.action ?? '—')}</span>
                    <span className="truncate font-mono text-muted-foreground">{String(e.actor_did ?? '—')}</span>
                    <span className="shrink-0 text-muted-foreground">{relativeTime(String(e.ts ?? ''))}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="space-y-2">
            <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Raw</h3>
            <JsonBlock value={task} />
          </section>
        </div>

        {operatorMode && (
          <div className="border-t border-border p-4">
            {atRest ? (
              !editing && (
                <div className="space-y-2">
                  {error && <p className="text-xs text-destructive">{error}</p>}
                  {task.status === 'review' && !confirmDelete && (
                    <div className="flex items-center gap-2">
                      <span className="mr-auto text-xs text-muted-foreground">Awaiting review</span>
                      <Button size="sm" disabled={deciding} onClick={() => decide(true)}>
                        {deciding ? '…' : 'Approve'}
                      </Button>
                      <Button size="sm" variant="outline" disabled={deciding} onClick={() => decide(false)}>
                        Reject
                      </Button>
                    </div>
                  )}
                  {confirmDelete ? (
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-xs text-muted-foreground">Delete this task permanently?</span>
                      <Button size="sm" variant="destructive" disabled={deleting} onClick={remove}>
                        {deleting ? 'Deleting…' : 'Confirm delete'}
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={deleting}
                        onClick={() => setConfirmDelete(false)}
                      >
                        Cancel
                      </Button>
                    </div>
                  ) : (
                    <div className="flex gap-2">
                      <Button size="sm" onClick={() => setEditing(true)}>Edit</Button>
                      <Button size="sm" variant="destructive" onClick={() => setConfirmDelete(true)}>
                        Delete
                      </Button>
                    </div>
                  )}
                </div>
              )
            ) : (
              <div className="space-y-2">
                {error && <p className="text-xs text-destructive">{error}</p>}
                <div className="flex items-center justify-between gap-2">
                  <div className="text-xs text-muted-foreground">
                    Steer owner{ownerAgent ? ` — @${ownerAgent.name}` : ''} ({steerStatus})
                  </div>
                  <Button size="sm" variant="destructive" disabled={stopping} onClick={stop}>
                    {stopping ? 'Stopping…' : 'Stop task'}
                  </Button>
                </div>
                <MentionComposer
                  value={steerText}
                  onChange={setSteerText}
                  onSubmit={sendSteer}
                  handles={mentionHandles}
                  placeholder="Message the owner…"
                  disabled={steerStatus !== 'ready'}
                />
              </div>
            )}
          </div>
        )}
      </SheetContent>
    </Sheet>
  )
}
