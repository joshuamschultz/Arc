import { useState } from 'react'
import { Check, GitPullRequestArrow, MessageSquareText, ShieldQuestion, XCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { FieldHelp } from '@/components/help'
import { Textarea } from '@/components/ui/textarea'
import { SchemaPayloadEditor } from '@/components/schema-payload-editor'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useResolveGate } from '@/lib/queries'
import { ApiError } from '@/lib/api'
import { EMPTY_DRAFT, draftPayload, problemsFromIssues, type PayloadDraft } from '@/lib/schema-form'
import type { GateDecision } from '@/lib/types'

/**
 * The in-channel answerable card for a workflow gate node (T-855, COMP-018).
 *
 * Relocates the `ApprovalCard` pattern (pages/approvals.tsx) into the
 * channel message stream, keyed to a review-status workflow node task
 * instead of a trifecta-block approval request. Resolution posts through
 * the control-plane-only `/api/workflow-tasks/{id}/gate` route — there is
 * no agent-callable path that can resolve a gate (REQ-246).
 *
 * A rejected gate is not a binary approve/deny like the trifecta approval
 * card: the reviewer additionally chooses between failing the whole run and
 * returning it for revision with notes (REQ-247), so this card has a third
 * button and a notes field that only appears for "return for revision".
 *
 * SPEC-088 (COMP-016): a step whose run node carries `outputSchema` asks for
 * a payload with its decision, rendered by `SchemaPayloadEditor` (a typed form
 * for a flat schema, raw JSON otherwise). A `human_input` step shows its
 * prompt and answers with Submit or Fail run; it is never approved or sent
 * back. A 422 lands next to the field it names and the step stays decidable.
 */
export function GateCard({
  taskId,
  nodeId,
  body,
  kind,
  prompt,
  outputSchema,
}: {
  taskId: string
  nodeId?: string
  body: string
  kind?: string | null
  prompt?: string | null
  outputSchema?: Record<string, unknown> | null
}) {
  const [operatorMode] = useOperatorMode()
  const resolveGate = useResolveGate(taskId)
  const [notesFor, setNotesFor] = useState<GateDecision | null>(null)
  const [notes, setNotes] = useState('')
  const [draft, setDraft] = useState<PayloadDraft>(EMPTY_DRAFT)
  const [problems, setProblems] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [resolved, setResolved] = useState<GateDecision | null>(null)

  const isInput = kind === 'human_input'
  const asksPayload = isInput || outputSchema != null
  const primary: GateDecision = isInput ? 'submit' : 'approve'

  const resolve = async (decision: GateDecision) => {
    setError(null)
    setProblems({})
    let payload: Record<string, unknown> | undefined
    if (decision === primary && asksPayload) {
      const read = draftPayload(outputSchema, draft)
      if ('problems' in read) return setProblems(read.problems)
      payload = read.payload
    }
    try {
      await resolveGate.mutateAsync({ decision, notes: notes.trim() || undefined, payload })
      setResolved(decision)
      setNotesFor(null)
    } catch (e) {
      if (e instanceof ApiError && e.status === 422 && e.errors?.length) {
        setProblems(problemsFromIssues(e.errors))
        setError('Fix the highlighted fields and try again.')
      } else {
        setError(e instanceof ApiError ? e.message : `Could not resolve (${decision.replace(/_/g, ' ')})`)
      }
    }
  }

  const busy = resolveGate.isPending
  const Icon = isInput ? MessageSquareText : ShieldQuestion

  return (
    <div className="rounded-lg border border-status-warning/30 bg-status-warning/5 p-4 shadow-xs">
      <div className="flex items-start gap-3">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-status-warning/15 text-status-warning">
          <Icon className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="font-semibold text-foreground">
              {isInput ? 'Waiting for your input' : 'Gate waiting on a decision'}
            </span>
            {nodeId && (
              <span className="rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-foreground">
                {nodeId}
              </span>
            )}
          </div>
          {isInput && prompt ? (
            <p className="mt-1.5 whitespace-pre-wrap text-[15px] leading-snug text-foreground">{prompt}</p>
          ) : (
            <p className="mt-1 text-sm text-foreground">{body}</p>
          )}

          {resolved ? (
            <p className="mt-3 text-xs font-medium text-status-online">
              Resolved: {resolved.replace(/_/g, ' ')}
            </p>
          ) : operatorMode ? (
            <div className="mt-3 space-y-3">
              {asksPayload && notesFor !== 'return_for_revision' && (
                <div className="rounded-md border border-border bg-background/60 p-3">
                  <SchemaPayloadEditor
                    schema={outputSchema}
                    draft={draft}
                    problems={problems}
                    onDraft={setDraft}
                  />
                </div>
              )}
              {notesFor === 'return_for_revision' && (
                <>
                  <Textarea
                    autoFocus
                    rows={2}
                    value={notes}
                    onChange={(e) => setNotes(e.target.value)}
                    placeholder="Notes for the revision (what needs to change)…"
                  />
                  <FieldHelp helpKey="workflow.gate.notes" route="messages" />
                </>
              )}
              <div className="flex flex-wrap items-center gap-2">
                <Button size="sm" disabled={busy} onClick={() => resolve(primary)}>
                  <Check className="size-3.5" /> {isInput ? 'Submit' : 'Approve'}
                </Button>
                {!isInput &&
                  (notesFor === 'return_for_revision' ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy || !notes.trim()}
                      onClick={() => resolve('return_for_revision')}
                    >
                      <GitPullRequestArrow className="size-3.5" /> Send back
                    </Button>
                  ) : (
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy}
                      onClick={() => setNotesFor('return_for_revision')}
                    >
                      <GitPullRequestArrow className="size-3.5" /> Return for revision
                    </Button>
                  ))}
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  onClick={() => resolve('fail_run')}
                  className="text-destructive hover:text-destructive"
                >
                  <XCircle className="size-3.5" /> Fail run
                </Button>
                {error && <span className="text-xs text-destructive">{error}</span>}
              </div>
            </div>
          ) : (
            <p className="mt-3 text-xs italic text-muted-foreground/80">
              Enable operator mode to resolve this {isInput ? 'step' : 'gate'}.
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
