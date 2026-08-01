import { useState } from 'react'
import { Check, GitPullRequestArrow, ShieldQuestion, XCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useResolveGate } from '@/lib/queries'
import { ApiError } from '@/lib/api'
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
 */
export function GateCard({
  taskId,
  nodeId,
  body,
}: {
  taskId: string
  nodeId?: string
  body: string
}) {
  const [operatorMode] = useOperatorMode()
  const resolveGate = useResolveGate(taskId)
  const [notesFor, setNotesFor] = useState<GateDecision | null>(null)
  const [notes, setNotes] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [resolved, setResolved] = useState<GateDecision | null>(null)

  const resolve = async (decision: GateDecision) => {
    setError(null)
    try {
      await resolveGate.mutateAsync({ decision, notes: notes.trim() || undefined })
      setResolved(decision)
      setNotesFor(null)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : `Could not resolve gate (${decision})`)
    }
  }

  const busy = resolveGate.isPending

  return (
    <div className="rounded-lg border border-status-warning/30 bg-status-warning/5 p-4 shadow-xs">
      <div className="flex items-start gap-3">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-status-warning/15 text-status-warning">
          <ShieldQuestion className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="font-semibold text-foreground">Gate waiting on a decision</span>
            {nodeId && (
              <span className="rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-foreground">
                {nodeId}
              </span>
            )}
          </div>
          <p className="mt-1 text-sm text-foreground">{body}</p>

          {resolved ? (
            <p className="mt-3 text-xs font-medium text-status-online">
              Resolved: {resolved.replace(/_/g, ' ')}
            </p>
          ) : operatorMode ? (
            <div className="mt-3 space-y-2">
              {notesFor === 'return_for_revision' && (
                <Textarea
                  autoFocus
                  rows={2}
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                  placeholder="Notes for the revision (what needs to change)…"
                />
              )}
              <div className="flex items-center gap-2">
                <Button size="sm" disabled={busy} onClick={() => resolve('approve')}>
                  <Check className="size-3.5" /> Approve
                </Button>
                {notesFor === 'return_for_revision' ? (
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
                )}
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
              Enable operator mode to resolve this gate.
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
