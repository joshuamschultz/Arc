import { useState } from 'react'
import { Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import { Archive, GitBranch, Plus } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { FieldHelp } from '@/components/help'
import { OperatorModeToggle } from '@/components/operator-mode-toggle'
import { AgentHandleSelect } from '@/components/agent-handle-select'
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
import { WorkflowCard, WorkflowSummaryStrip } from '@/components/workflows-view/workflow-card'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import {
  useCreateWorkflow,
  useCreateWorkflowFromTemplate,
  useWorkflowRun,
  useWorkflows,
  useWorkflowTemplates,
} from '@/lib/queries'
import { ApiError } from '@/lib/api'
import { runPath } from '@/lib/workflow-paths'

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
  const fromTemplate = useCreateWorkflowFromTemplate()
  const templates = useWorkflowTemplates(open)
  const [name, setName] = useState('')
  const [template, setTemplate] = useState('')
  const [owner, setOwner] = useState('')
  const [error, setError] = useState<string | null>(null)

  const reset = () => {
    setName('')
    setTemplate('')
    setOwner('')
    setError(null)
  }

  const submit = async () => {
    setError(null)
    try {
      const id = template
        ? (await fromTemplate.mutateAsync({ template, workflow_id: name.trim(), owner })).workflow_id
        : (await createWorkflow.mutateAsync({ name: name.trim(), owner })).id
      reset()
      onOpenChange(false)
      navigate(`/workflows/${encodeURIComponent(id)}`)
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
            <FieldHelp helpKey="workflow.search" route="workflows" />
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="onboarding" />
          </div>
          {(templates.data?.templates.length ?? 0) > 0 && (
            <div className="space-y-1.5">
              <label
                htmlFor="workflow-template"
                className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground"
              >
                Start from template
              </label>
              <select
                id="workflow-template"
                value={template}
                onChange={(e) => setTemplate(e.target.value)}
                className="h-9 w-full rounded-md border border-input bg-transparent px-2 text-sm"
              >
                <option value="">Empty draft</option>
                {templates.data!.templates.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.title}
                  </option>
                ))}
              </select>
              {template && (
                <p className="text-xs text-muted-foreground">
                  {templates.data!.templates.find((t) => t.id === template)?.description}
                </p>
              )}
            </div>
          )}
          <div className="space-y-1.5">
            <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Owner
            </span>
            <AgentHandleSelect label="Owner agent" value={owner} onChange={setOwner} />
            <p className="text-xs text-muted-foreground">
              The agent that runs every step you do not name another for. A workflow with no owner
              cannot be signed.
            </p>
          </div>
          <Button
            className="w-full"
            disabled={createWorkflow.isPending || fromTemplate.isPending || !name.trim() || !owner}
            onClick={submit}
          >
            {createWorkflow.isPending || fromTemplate.isPending ? 'Creating…' : 'Create draft'}
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  )
}

/** `?run=<run_id>` (audit and notice deep links) opens that run's own page. */
function useLinkedRunTarget(): string | null {
  const [searchParams] = useSearchParams()
  const runId = searchParams.get('run')
  const run = useWorkflowRun(runId)
  if (!runId || !run.data?.workflow_id) return null
  return runPath(run.data.workflow_id, runId)
}

export function WorkflowsPage() {
  const linkedRunTarget = useLinkedRunTarget()
  const [showArchived, setShowArchived] = useState(false)
  const workflows = useWorkflows(showArchived)
  const [operatorMode] = useOperatorMode()
  const [creating, setCreating] = useState(false)

  if (linkedRunTarget) return <Navigate to={linkedRunTarget} replace />

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
            <FieldHelp helpKey="workflow.archived" route="workflows" />
            <OperatorModeToggle />
            {operatorMode && (
              <Button size="sm" onClick={() => setCreating(true)}>
                <Plus className="size-3.5" /> New workflow
              </Button>
            )}
          </div>
        }
      />
      <div className="flex-1 overflow-auto p-4 md:p-6">
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
            <div className="mx-auto flex max-w-5xl flex-col gap-4">
              <WorkflowSummaryStrip workflows={data.workflows} />
              <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                {data.workflows.map((w) => (
                  <WorkflowCard key={w.id} w={w} />
                ))}
              </div>
            </div>
          )}
        </QueryState>
      </div>
      <CreateWorkflowSheet open={creating} onOpenChange={setCreating} />
    </div>
  )
}
