import { useState } from 'react'
import { Trash2 } from 'lucide-react'
import { QueryState } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { fmtTime } from '@/lib/format'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import {
  useDemoteShared,
  useSharedDetail,
  type SharedDetail,
  type SharedProvenance,
} from '@/lib/queries-shared'

const MAX_REASON = 500

/** Data-sensitivity pill. Renders nothing for an unclassified card: an
 *  "unclassified" or "unlabeled" badge would claim a label nobody assigned. */
export function ClassificationBadge({ classification }: { classification: string }) {
  const label = classification.trim()
  if (label === '' || label.toLowerCase() === 'unclassified') return null
  return (
    <span className="rounded-full border border-status-warning/40 bg-status-warning/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-status-warning">
      {label}
    </span>
  )
}

const DECISION_WORDS: Record<SharedProvenance['decision'], string> = {
  classifier_promote: 'Promoted by classifier',
  operator_promote: 'Promoted by operator',
  direct: 'Promoted directly by the owner',
}

function ProvenanceRow({ row }: { row: SharedProvenance }) {
  const verdict = [
    row.classifier_version,
    row.confidence != null ? `${Math.round(row.confidence * 100)}%` : null,
  ]
    .filter(Boolean)
    .join(' · ')
  return (
    <li className="space-y-0.5 rounded-md border border-border bg-muted/30 px-3 py-2 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium text-foreground">{DECISION_WORDS[row.decision]}</span>
        <span className="text-muted-foreground">{fmtTime(row.promoted_at)}</span>
      </div>
      <div className="text-muted-foreground">From {row.contributor_display}</div>
      <div className="font-mono text-muted-foreground">{row.source_ref}</div>
      {verdict && <div className="text-muted-foreground">{verdict}</div>}
      {row.decided_by && <div className="text-muted-foreground">Decided by {row.decided_by}</div>}
    </li>
  )
}

/** Operator-only permanent removal. Needs a reason and an explicit confirm. */
function RemoveFromFleet({ identifier, onRemoved }: { identifier: string; onRemoved: () => void }) {
  const [confirming, setConfirming] = useState(false)
  const [reason, setReason] = useState('')
  const demote = useDemoteShared(identifier)
  const trimmed = reason.trim()

  if (!confirming) {
    return (
      <Button type="button" size="sm" variant="outline" onClick={() => setConfirming(true)}>
        <Trash2 className="size-3.5" />
        Remove from fleet
      </Button>
    )
  }
  return (
    <div role="alertdialog" aria-label="Confirm removal" className="space-y-2 rounded-md border border-destructive/40 bg-destructive/5 p-3">
      <p className="text-xs text-foreground">
        This is permanent. The card will never be re-shared to the fleet, by anyone.
      </p>
      <label htmlFor={`demote-reason-${identifier}`} className="block text-xs font-medium text-foreground">
        Reason (required)
      </label>
      <Textarea
        id={`demote-reason-${identifier}`}
        value={reason}
        maxLength={MAX_REASON}
        rows={2}
        onChange={(e) => setReason(e.target.value)}
      />
      {demote.isError && (
        <p role="alert" className="text-xs text-status-error">
          {demote.error.message}
        </p>
      )}
      <div className="flex gap-2">
        <Button
          type="button"
          size="sm"
          variant="destructive"
          disabled={trimmed === '' || demote.isPending}
          onClick={() => demote.mutate(trimmed, { onSuccess: onRemoved })}
        >
          Remove permanently
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => setConfirming(false)}>
          Cancel
        </Button>
      </div>
    </div>
  )
}

function DocumentBody({ data, onRemoved }: { data: SharedDetail; onRemoved: () => void }) {
  const [operatorMode] = useOperatorMode()
  return (
    <>
      <SheetHeader className="border-b border-border px-5 py-4">
        <SheetTitle className="text-sm">{data.title}</SheetTitle>
        <SheetDescription className="flex flex-wrap items-center gap-2">
          <span className="rounded-full border border-border bg-muted/40 px-2 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            {data.kind}
          </span>
          <ClassificationBadge classification={data.classification} />
          {data.tags.map((tag) => (
            <span
              key={tag}
              className="rounded-full border border-border bg-muted/40 px-2 py-0.5 text-[10px] text-muted-foreground"
            >
              {tag}
            </span>
          ))}
        </SheetDescription>
      </SheetHeader>
      <div className="flex-1 space-y-5 overflow-auto px-5 py-4">
        <p className="whitespace-pre-wrap text-sm text-foreground">{data.content}</p>
        <section className="space-y-2">
          <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            Provenance
          </h3>
          <ul className="space-y-2">
            {data.provenance.map((row) => (
              <ProvenanceRow key={`${row.source_ref}:${row.digest}`} row={row} />
            ))}
          </ul>
        </section>
        {operatorMode && <RemoveFromFleet identifier={data.identifier} onRemoved={onRemoved} />}
      </div>
    </>
  )
}

/** Full-content drawer for one shared document: content, provenance, Remove.
 *  403 (above clearance) and 404 (missing or removed) surface via `QueryState`. */
export function SharedDocumentSheet({
  identifier,
  onClose,
}: {
  identifier: string | null
  onClose: () => void
}) {
  const query = useSharedDetail(identifier)
  return (
    <Sheet open={identifier != null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-xl">
        {identifier && (
          <QueryState query={query}>
            {(data) => <DocumentBody data={data} onRemoved={onClose} />}
          </QueryState>
        )}
      </SheetContent>
    </Sheet>
  )
}
