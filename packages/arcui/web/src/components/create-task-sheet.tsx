import { useState } from 'react'
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
import { apiPost, ApiError } from '@/lib/api'
import type { Agent, Task, TaskPriority } from '@/lib/types'

const PRIORITIES: TaskPriority[] = ['low', 'medium', 'high', 'critical']

/** Operator-only create-task form (SPEC-056 D4, FR-7). */
export function CreateTaskSheet({
  open,
  onOpenChange,
  roster,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  roster: Agent[]
}) {
  const queryClient = useQueryClient()
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [priority, setPriority] = useState<TaskPriority>('medium')
  const [ownerDid, setOwnerDid] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const reset = () => {
    setTitle('')
    setDescription('')
    setPriority('medium')
    setOwnerDid('')
    setError(null)
  }

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await apiPost<Task>('/api/team/tasks', {
        title: title.trim(),
        description: description.trim() || undefined,
        priority,
        owner_did: ownerDid || undefined,
      })
      await queryClient.invalidateQueries({
        predicate: (q) => q.queryKey.some((k) => k === 'tasks'),
      })
      reset()
      onOpenChange(false)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to create task')
    } finally {
      setBusy(false)
    }
  }

  const handleOpenChange = (o: boolean) => {
    onOpenChange(o)
    if (!o) reset()
  }

  return (
    <Sheet open={open} onOpenChange={handleOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-md">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="text-sm">New task</SheetTitle>
          <SheetDescription>
            Unowned tasks land in the fleet backlog; assigning an owner starts them in that
            agent's to-do.
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 space-y-4 overflow-auto p-5">
          {error && (
            <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}
          <div className="space-y-1.5">
            <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Title</label>
            <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Investigate the outage" />
          </div>
          <div className="space-y-1.5">
            <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Description (optional)</label>
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
              <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Owner (optional)</label>
              <Select value={ownerDid || '__none__'} onValueChange={(v) => setOwnerDid(v === '__none__' ? '' : v)}>
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
          <Button className="w-full" disabled={busy || !title.trim()} onClick={submit}>
            {busy ? 'Creating…' : 'Create task'}
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  )
}
