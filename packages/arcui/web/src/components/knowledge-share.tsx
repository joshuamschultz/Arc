import { useState } from 'react'
import { History, Share2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { QueryState } from '@/components/states'
import { ApiError } from '@/lib/api'
import { fmtTime } from '@/lib/format'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import {
  useCardDecisions,
  useShareToFleet,
  type CardDecision,
  type ShareResponse,
  type SharedKind,
} from '@/lib/queries-shared'

/** Plain-language outcome of a share the server accepted (200 or 202). */
function shareOutcomeMessage(result: ShareResponse): { ok: boolean; text: string } {
  if (result.status === 'outcome_unknown') {
    return {
      ok: false,
      text: 'Arc could not confirm the share finished. Check Shared knowledge before trying again.',
    }
  }
  return { ok: true, text: 'Shared to the fleet.' }
}

const REFUSALS: Record<string, string> = {
  blocked_secret:
    'Blocked: this card contains a secret. A secret can never be shared, and there is no override.',
  too_large: 'Blocked: this card is too large to share.',
  demoted: 'An operator removed this card from the fleet. It can never be shared again.',
  refused: 'The fleet refused this card, so it was not shared.',
  disabled: 'Memory sharing is turned off for this agent.',
  tier_forbidden: 'Sharing is not allowed at this deployment tier.',
  clearance_refused: 'Sharing is not allowed: the card is above this agent’s clearance.',
  publisher_unavailable: 'The fleet knowledge store is unavailable. Try again later.',
  not_found: 'This card no longer exists.',
}

/** Turn a failed share into an honest sentence, keyed on the server's status code. */
function shareErrorMessage(error: Error): string {
  if (!(error instanceof ApiError)) return error.message
  const code = typeof error.body?.status === 'string' ? error.body.status : ''
  if (REFUSALS[code]) return REFUSALS[code]
  if (error.status === 403) return 'Sharing is not allowed. An operator role is required.'
  return error.message
}

const DECISION_LABELS: Record<string, string> = {
  promote: 'Shared by classifier',
  keep_private: 'Kept private by classifier',
  blocked_secret: 'Blocked: contains a secret',
  too_large: 'Blocked: too large',
  promoted_by_operator: 'Shared by operator',
  demoted_by_operator: 'Removed by operator',
}

function DecisionRow({ row }: { row: CardDecision }) {
  return (
    <li className="space-y-0.5 rounded-md border border-border bg-muted/30 px-2.5 py-1.5 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium text-foreground">
          {DECISION_LABELS[row.decision] ?? row.decision}
        </span>
        <span className="text-muted-foreground">{fmtTime(row.evaluated_at)}</span>
      </div>
      {row.classifier_version && (
        <div className="text-muted-foreground">
          {row.classifier_version}
          {row.confidence != null && ` · ${Math.round(row.confidence * 100)}% sure`}
        </div>
      )}
      {row.decided_by && <div className="text-muted-foreground">by {row.decided_by}</div>}
      {row.reason && <div className="text-foreground">{row.reason}</div>}
    </li>
  )
}

function DecisionsList({
  agentId,
  kind,
  itemId,
}: {
  agentId: string
  kind: SharedKind
  itemId: string
}) {
  const query = useCardDecisions(agentId, kind, itemId, true)
  return (
    <QueryState
      query={query}
      isEmpty={(d) => d.decisions.length === 0}
      empty={<p className="text-xs text-muted-foreground">No decisions recorded for this card.</p>}
    >
      {(data) => (
        <ul className="space-y-1.5">
          {data.decisions.map((row, i) => (
            <DecisionRow key={i} row={row} />
          ))}
        </ul>
      )}
    </QueryState>
  )
}

/**
 * "Share to fleet" (operator only) and "Decisions" (any role) for one memory
 * card. The server is the real gate; operator mode only hides the button.
 */
export function CardShareActions({
  agentId,
  kind,
  itemId,
}: {
  agentId: string
  kind: SharedKind
  itemId: string
}) {
  const [operatorMode] = useOperatorMode()
  const [showDecisions, setShowDecisions] = useState(false)
  const share = useShareToFleet(agentId, kind, itemId)
  const outcome = share.data ? shareOutcomeMessage(share.data) : null

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {operatorMode && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={share.isPending}
            onClick={() => share.mutate()}
          >
            <Share2 className="size-3.5" />
            Share to fleet
          </Button>
        )}
        <Button
          type="button"
          size="sm"
          variant="ghost"
          aria-expanded={showDecisions}
          onClick={() => setShowDecisions((v) => !v)}
        >
          <History className="size-3.5" />
          Decisions
        </Button>
      </div>
      {outcome && (
        <p
          role="status"
          className={outcome.ok ? 'text-xs text-status-online' : 'text-xs text-status-warning'}
        >
          {outcome.text}
        </p>
      )}
      {share.isError && (
        <p role="alert" className="text-xs text-status-error">
          {shareErrorMessage(share.error)}
        </p>
      )}
      {showDecisions && <DecisionsList agentId={agentId} kind={kind} itemId={itemId} />}
    </div>
  )
}
