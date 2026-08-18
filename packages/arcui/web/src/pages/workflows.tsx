import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Archive, GitBranch, Plus } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { OperatorModeToggle } from '@/components/operator-mode-toggle'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { QueryState, EmptyState } from '@/components/states'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useCreateWorkflow, useWorkflows } from '@/lib/queries'
import { ApiError } from '@/lib/api'
import { fmtTime } from '@/lib/format'
import type { WorkflowSummary } from '@/lib/types'

const STATUS_TONE: Record<string, string> = {
  draft: 'border-status-warning/30 bg-status-warning/10 text-status-warning',
  signed: 'border-status-online/30 bg-status-online/10 text-status-online',
  archived: 'border-border bg-muted/30 text-muted-foreground',
}

function StatusPill({ status }: { status: string }) {
  return (
    <span
      className={`inline-flex items-center rounded-md border px-1.5 py-0.5 text-[11px] font-medium capitalize ${
        STATUS_TONE[status] ?? 'border-border bg-muted/30 text-muted-foreground'
      }`}
    >
      {status}
    </span>
  )
}

/** Operator-only create-workflow form — a name + optional trigger JSON.
 *
 * A full node/edge editor lives on the detail page (DESIGN.md §8's open
 * question resolves toward node-property editing over a rendered graph, not
 * drag-and-drop authoring) — this sheet only starts the empty draft the
 * control plane always produces (SDD COMP-005: "Always produces drafts").
 */
function CreateWorkflowSheet({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const navigate = useNavigate()
  const createWorkflow = useCreateWorkflow()
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)

  const reset = () => {
    setName('')
    setError(null)
  }

  const submit = async () => {
    setError(null)
    try {
      const created = await createWorkflow.mutateAsync({ name: name.trim() })
      reset()
      onOpenChange(false)
      navigate(`/workflows/${encodeURIComponent(created.id)}`)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to create workflow')
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
          <SheetTitle className="text-sm">New workflow</SheetTitle>
          <SheetDescription>
            Starts an empty draft — add nodes, edges, a trigger, and a channel from the editor.
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 space-y-4 overflow-auto p-5">
          {error && (
            <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}
          <div className="space-y-1.5">
            <label className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Name
            </label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="onboarding" />
          </div>
          <Button className="w-full" disabled={createWorkflow.isPending || !name.trim()} onClick={submit}>
            {createWorkflow.isPending ? 'Creating…' : 'Create draft'}
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  )
}

function WorkflowRow({ w }: { w: WorkflowSummary }) {
  const navigate = useNavigate()
  const lastRun = w.last_run
  return (
    <button
      type="button"
      onClick={() => navigate(`/workflows/${encodeURIComponent(w.id)}`)}
      className="flex w-full items-center gap-3 rounded-lg border border-border bg-card px-4 py-3 text-left transition-colors hover:bg-muted/40"
    >
      <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-primary/15 text-primary">
        <GitBranch className="size-4" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-semibold text-foreground">{w.name || w.id}</span>
          <StatusPill status={w.status} />
          <span className="text-xs text-muted-foreground">v{w.version}</span>
        </div>
        {w.trigger && (
          <p className="mt-0.5 text-xs text-muted-foreground">
            trigger: {String((w.trigger as Record<string, unknown>).type ?? 'manual')}
          </p>
        )}
      </div>
      <div className="shrink-0 text-right text-xs text-muted-foreground">
        {lastRun ? (
          <>
            <div className="capitalize">{lastRun.status.replace(/_/g, ' ')}</div>
            {lastRun.ended_at && <div>{fmtTime(lastRun.ended_at)}</div>}
          </>
        ) : (
          <span>No runs yet</span>
        )}
      </div>
    </button>
  )
}

export function WorkflowsPage() {
  const [showArchived, setShowArchived] = useState(false)
  const workflows = useWorkflows(showArchived)
  const [operatorMode] = useOperatorMode()
  const [creating, setCreating] = useState(false)

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Workflows"
        description="Named, signed, conversationally-authored multi-agent workflows."
        actions={
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant={showArchived ? 'secondary' : 'ghost'}
              onClick={() => setShowArchived((v) => !v)}
              aria-pressed={showArchived}
              title={showArchived ? 'Hide archived workflows' : 'Show archived workflows'}
            >
              <Archive className="size-3.5" /> {showArchived ? 'Hide archived' : 'Show archived'}
            </Button>
            <OperatorModeToggle />
            {operatorMode && (
              <Button size="sm" onClick={() => setCreating(true)}>
                <Plus className="size-3.5" /> New workflow
              </Button>
            )}
          </div>
        }
      />
      <div className="flex-1 overflow-auto p-6">
        <QueryState
          query={workflows}
          isEmpty={(data) => data.workflows.length === 0}
          empty={
            <EmptyState
              icon={<GitBranch className="size-7" />}
              title="No workflows yet"
              description="Create one from the dashboard, the CLI, or by asking an agent to build it."
            />
          }
        >
          {(data) => (
            <div className="mx-auto flex max-w-3xl flex-col gap-2">
              {data.workflows.map((w) => (
                <WorkflowRow key={w.id} w={w} />
              ))}
            </div>
          )}
        </QueryState>
      </div>
      <CreateWorkflowSheet open={creating} onOpenChange={setCreating} />
    </div>
  )
}
