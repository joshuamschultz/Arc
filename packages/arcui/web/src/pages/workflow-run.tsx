import { Link, useParams } from 'react-router-dom'
import { ChevronLeft, StopCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { QueryState } from '@/components/states'
import { StatusText } from '@/components/status-badge'
import { RunTimeline } from '@/components/workflows-view/run-timeline'
import { useCancelWorkflowRun, useWorkflow, useWorkflowRun } from '@/lib/queries'
import { shortId } from '@/lib/format'

const ACTIVE = new Set(['pending', 'running', 'waiting_gate'])

/** One workflow run on its own page: why it failed, and every node's timeline. */
export function WorkflowRunPage() {
  const { id = '', runId = '' } = useParams()
  const workflow = useWorkflow(id)
  const run = useWorkflowRun(runId)
  const cancelRun = useCancelWorkflowRun(runId)
  const active = run.data ? ACTIVE.has(run.data.status) : false

  return (
    <div className="space-y-3 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <Link
            to={`/workflows/${encodeURIComponent(id)}?tab=runs`}
            className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          >
            <ChevronLeft className="size-4" />
            {workflow.data?.name || id}
          </Link>
          <span className="font-mono text-xs text-muted-foreground">{shortId(runId, 18)}</span>
          {run.data && <StatusText value={run.data.status} />}
        </div>
        {active && (
          <Button
            size="sm"
            variant="ghost"
            className="text-destructive hover:text-destructive"
            disabled={cancelRun.isPending}
            onClick={() => cancelRun.mutate()}
          >
            <StopCircle className="size-3.5" /> Cancel run
          </Button>
        )}
      </div>
      <QueryState query={workflow}>
        {(data) => <RunTimeline workflow={data} runId={runId} />}
      </QueryState>
    </div>
  )
}
