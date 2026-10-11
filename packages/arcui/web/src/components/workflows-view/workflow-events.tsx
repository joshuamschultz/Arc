import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Zap } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { ApiError } from '@/lib/api'
import { useFireWorkflowEvent } from '@/lib/queries'
import { JSON_PROBLEM, jsonPayload } from '@/lib/schema-form'
import { runPath } from '@/lib/workflow-paths'
import type { WorkflowDetail, WorkflowEvent } from '@/lib/types'

type Outcome =
  | { kind: 'started'; runId: string; replayed: boolean }
  | { kind: 'refused'; message: string; issues: Array<{ field: string; error: string }> }

function outcomeOf(e: unknown): Outcome {
  if (e instanceof ApiError && e.errors?.length) {
    const issues = e.errors.map((i) => ({ field: String(i.field ?? ''), error: String(i.error ?? '') }))
    return { kind: 'refused', message: 'The payload does not match the event.', issues }
  }
  return { kind: 'refused', message: e instanceof Error ? e.message : 'The event was not fired.', issues: [] }
}

/** The "Fire event" dialog: a JSON payload, sent with a fresh idempotency key per Fire. */
function FireEventSheet({
  workflowId,
  event,
  onClose,
}: {
  workflowId: string
  event: WorkflowEvent
  onClose: () => void
}) {
  const fire = useFireWorkflowEvent(workflowId)
  const [text, setText] = useState('{}')
  const [outcome, setOutcome] = useState<Outcome | null>(null)

  const submit = async () => {
    const read = jsonPayload(text)
    if ('problems' in read) {
      return setOutcome({ kind: 'refused', message: read.problems[JSON_PROBLEM], issues: [] })
    }
    setOutcome(null)
    try {
      const result = await fire.mutateAsync({ event: event.name, payload: read.payload })
      setOutcome({ kind: 'started', runId: result.run.run_id, replayed: result.replayed })
    } catch (e) {
      setOutcome(outcomeOf(e))
    }
  }

  return (
    <Sheet open onOpenChange={(open) => !open && onClose()}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-md">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle>
            Fire <span className="font-mono">{event.name}</span>
          </SheetTitle>
          <SheetDescription>
            Starts a run of this workflow with the payload below. Each Fire is a new request, so
            each one starts its own run.
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 space-y-3 overflow-auto px-5 py-4">
          <label htmlFor="fire-event-payload" className="block text-xs font-medium text-foreground">
            Event payload (JSON)
          </label>
          <Textarea
            id="fire-event-payload"
            rows={8}
            spellCheck={false}
            className="max-h-80 min-h-40 font-mono text-xs"
            aria-invalid={outcome?.kind === 'refused' || undefined}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          {event.schema ? (
            <p className="text-xs text-muted-foreground">
              Checked against <span className="font-mono">{event.schema}</span> before any run starts.
            </p>
          ) : (
            <p className="text-xs text-muted-foreground">This event takes any JSON object.</p>
          )}
          {outcome?.kind === 'started' && (
            <div role="status" className="flex flex-wrap items-baseline gap-x-3 text-sm text-status-online">
              <span>
                {outcome.replayed
                  ? `Already started run ${outcome.runId}`
                  : `Started run ${outcome.runId}`}
              </span>
              <Link className="text-xs underline" to={runPath(workflowId, outcome.runId)}>
                Open the run
              </Link>
            </div>
          )}
          {outcome?.kind === 'refused' && (
            <div role="alert" className="space-y-1 text-sm text-destructive">
              <p>{outcome.message}</p>
              {outcome.issues.length > 0 && (
                <ul className="list-disc space-y-0.5 pl-5 text-xs">
                  {outcome.issues.map((issue) => (
                    <li key={`${issue.field}:${issue.error}`}>
                      {issue.field}: {issue.error}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
        <div className="flex gap-2 border-t border-border px-5 py-3">
          <Button size="sm" disabled={fire.isPending} onClick={() => void submit()}>
            <Zap className="size-3.5" /> {fire.isPending ? 'Firing…' : 'Fire'}
          </Button>
          <Button size="sm" variant="ghost" onClick={onClose}>
            Close
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  )
}

/** The events a workflow declares, each with a "Fire event" action (SPEC-088 REQ-610). */
export function WorkflowEventsTab({ workflow }: { workflow: WorkflowDetail }) {
  const [firing, setFiring] = useState<WorkflowEvent | null>(null)
  const events = workflow.events ?? []

  return (
    <div className="max-w-3xl space-y-3">
      <p className="text-sm text-muted-foreground">
        An app or a person starts a run by firing one of these. The payload becomes the run&rsquo;s
        input.
      </p>
      <ul className="divide-y divide-border rounded-lg border border-border">
        {events.map((event) => (
          <li key={event.name} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3">
            <div className="min-w-0 flex-1">
              <div className="font-mono text-sm text-foreground">{event.name}</div>
              <div className="font-mono text-[11px] text-muted-foreground">
                {event.schema ?? 'any JSON object'}
              </div>
            </div>
            <Button size="sm" variant="outline" onClick={() => setFiring(event)}>
              <Zap className="size-3.5" /> Fire event
            </Button>
          </li>
        ))}
      </ul>
      {firing && (
        <FireEventSheet
          key={firing.name}
          workflowId={workflow.id}
          event={firing}
          onClose={() => setFiring(null)}
        />
      )}
    </div>
  )
}
