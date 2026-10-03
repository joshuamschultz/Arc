import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import { PulseCheckForm } from '@/components/pulse-check-form'
import { QueryState } from '@/components/states'
import { ApiError, apiDelete, apiGet, apiPost } from '@/lib/api'
import { pulseKey, pulsePath, type PulseCheckDraft } from '@/lib/pulse'
import { humanizeInterval } from '@/lib/schedule-format'
import { cn } from '@/lib/utils'

type PulseStatus = 'approved' | 'unapproved' | 'changes_pending'

type PulseCheck = {
  name: string
  interval_minutes: number
  action: string
  definition_digest: string
  status: PulseStatus
  approved: boolean
  stale: boolean
  approved_revision: number | null
  last_revision_ran: number | null
  diff: string
}

type PulseProposal = { name: string; interval_minutes: number; action: string; reason: string }

type PulseListing = {
  checks: PulseCheck[]
  proposals?: PulseProposal[]
  authority_available: boolean
}

const STATUS_LABEL: Record<PulseStatus, string> = {
  approved: 'Approved',
  unapproved: 'Pending approval',
  changes_pending: 'Changes pending approval',
}

const STATUS_STYLE: Record<PulseStatus, string> = {
  approved: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
  unapproved: 'border-border bg-muted/40 text-muted-foreground',
  changes_pending: 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400',
}

const errorText = (e: Error) => (e instanceof ApiError ? e.message : 'The request failed')

const EXAMPLE_ACTION = 'look for customer emails nobody has answered in a day and tell me who is waiting.'

/** Diff lines coloured by their +/- marker; context lines stay muted. */
function DiffBlock({ diff }: { diff: string }) {
  return (
    <pre className="overflow-x-auto rounded-md border border-border bg-muted/30 p-2 font-mono text-xs">
      {diff.split('\n').map((line, i) => (
        <div
          key={i}
          className={cn(
            line.startsWith('+') && !line.startsWith('+++') && 'text-emerald-700 dark:text-emerald-400',
            line.startsWith('-') && !line.startsWith('---') && 'text-destructive',
            (line.startsWith('@@') || line.startsWith('---') || line.startsWith('+++')) &&
              'text-muted-foreground',
          )}
        >
          {line}
        </div>
      ))}
    </pre>
  )
}

function CheckControls({
  agentId,
  check,
  onEdit,
  onError,
}: {
  agentId: string
  check: PulseCheck
  onEdit: (draft: PulseCheckDraft) => void
  onError: (message: string) => void
}) {
  const queryClient = useQueryClient()
  const [confirming, setConfirming] = useState(false)
  const remove = useMutation<unknown, Error, void>({
    // The digest is the one this row displayed, so a change since is refused server-side.
    mutationFn: () =>
      apiDelete(`${pulsePath(agentId)}/${encodeURIComponent(check.name)}`, {
        definition_digest: check.definition_digest,
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: pulseKey(agentId) }),
    onError: (e) => onError(errorText(e)),
  })

  return (
    <div className="flex flex-wrap gap-2">
      <Button
        size="sm"
        variant="outline"
        aria-label={`Edit ${check.name}`}
        onClick={() =>
          onEdit({
            name: check.name,
            interval_minutes: check.interval_minutes,
            action: check.action,
            definition_digest: check.definition_digest,
          })
        }
      >
        Edit
      </Button>
      {confirming ? (
        <>
          <Button
            size="sm"
            variant="destructive"
            aria-label={`Confirm delete ${check.name}`}
            disabled={remove.isPending}
            onClick={() => {
              onError('')
              remove.mutate()
            }}
          >
            Confirm delete
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setConfirming(false)}>
            Keep
          </Button>
        </>
      ) : (
        <Button
          size="sm"
          variant="outline"
          aria-label={`Delete ${check.name}`}
          onClick={() => setConfirming(true)}
        >
          Delete
        </Button>
      )}
    </div>
  )
}

function CheckRow({
  agentId,
  check,
  operatorMode,
  authorityAvailable,
  onEdit,
}: {
  agentId: string
  check: PulseCheck
  operatorMode: boolean
  authorityAvailable: boolean
  onEdit: (draft: PulseCheckDraft) => void
}) {
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const approve = useMutation<unknown, Error, void>({
    // The digest is the one this row displayed, so an edit since is refused server-side.
    mutationFn: () =>
      apiPost(`${pulsePath(agentId)}/approve`, {
        check: check.name,
        definition_digest: check.definition_digest,
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: pulseKey(agentId) }),
    onError: (e) => setError(errorText(e)),
  })

  return (
    <li className="space-y-2 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-foreground">{check.name}</span>
          <span className="text-xs text-muted-foreground">
            {humanizeInterval(check.interval_minutes * 60)}
          </span>
          <span
            className={cn('rounded-sm border px-1.5 py-0.5 text-[11px] font-medium', STATUS_STYLE[check.status])}
          >
            {STATUS_LABEL[check.status]}
          </span>
        </div>
        <span className="text-xs text-muted-foreground">
          {check.last_revision_ran == null
            ? 'Never ran'
            : `Last ran revision ${check.last_revision_ran}`}
        </span>
      </div>
      {!check.approved && (
        <>
          <p className="text-xs text-muted-foreground">
            {check.stale ? 'Changed since the last approved revision:' : 'Never approved. Review before approving:'}
          </p>
          <DiffBlock diff={check.diff || `+action: ${check.action}`} />
          {operatorMode && (
            <Button
              size="sm"
              onClick={() => {
                setError(null)
                approve.mutate()
              }}
              disabled={approve.isPending || !authorityAvailable}
              aria-label={`Approve ${check.name}`}
            >
              {approve.isPending ? 'Approving…' : 'Approve'}
            </Button>
          )}
        </>
      )}
      {operatorMode && (
        <CheckControls agentId={agentId} check={check} onEdit={onEdit} onError={setError} />
      )}
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </li>
  )
}

function ProposalRow({
  agentId,
  proposal,
  onReview,
}: {
  agentId: string
  proposal: PulseProposal
  onReview: (draft: PulseCheckDraft) => void
}) {
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const dismiss = useMutation<unknown, Error, void>({
    mutationFn: () =>
      apiDelete(`${pulsePath(agentId)}/proposals/${encodeURIComponent(proposal.name)}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: pulseKey(agentId) }),
    onError: (e) => setError(errorText(e)),
  })
  return (
    <li className="space-y-1 rounded-md border border-dashed border-border p-3">
      <p className="text-sm font-medium text-foreground">
        {proposal.name}{' '}
        <span className="text-xs font-normal text-muted-foreground">
          {humanizeInterval(proposal.interval_minutes * 60)}
        </span>
      </p>
      <p className="text-sm text-muted-foreground">{proposal.action}</p>
      {proposal.reason && <p className="text-xs italic text-muted-foreground">{proposal.reason}</p>}
      <div className="flex gap-2 pt-1">
        <Button
          size="sm"
          aria-label={`Review ${proposal.name}`}
          onClick={() =>
            onReview({
              name: proposal.name,
              interval_minutes: proposal.interval_minutes,
              action: proposal.action,
              proposal: proposal.name,
            })
          }
        >
          Review
        </Button>
        <Button
          size="sm"
          variant="ghost"
          aria-label={`Dismiss ${proposal.name}`}
          onClick={() => {
            setError(null)
            dismiss.mutate()
          }}
        >
          Dismiss
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </li>
  )
}

function EmptyState({ operatorMode, onAdd }: { operatorMode: boolean; onAdd: () => void }) {
  return (
    <div className="space-y-2">
      <p className="text-sm text-muted-foreground">No pulse checks yet.</p>
      <p className="text-sm text-muted-foreground">
        Pulse is a set of periodic checks the agent runs on its own, on a timer, without being
        asked. Each check is a short instruction and an interval.
      </p>
      <p className="text-sm text-muted-foreground">For example: every hour, {EXAMPLE_ACTION}</p>
      {operatorMode && (
        <Button size="sm" onClick={onAdd}>
          Add check
        </Button>
      )}
    </div>
  )
}

type FormState = { draft: PulseCheckDraft | null; editing: boolean }

/**
 * "Pulse checks" — add, edit and delete the checks the agent runs on its own, and
 * approve them. A check runs only after you approve its exact text, so every add or
 * edit returns to "pending approval". The agent may propose a check but never writes
 * pulse.md. Viewers see the state read-only.
 */
export function PulsePanel({ agentId, operatorMode }: { agentId: string; operatorMode: boolean }) {
  const query = useQuery<PulseListing>({
    queryKey: pulseKey(agentId),
    queryFn: ({ signal }) => apiGet<PulseListing>(pulsePath(agentId), signal),
  })
  const [form, setForm] = useState<FormState | null>(null)
  const [saved, setSaved] = useState(false)

  const openForm = (draft: PulseCheckDraft | null, editing: boolean) => {
    setSaved(false)
    setForm({ draft, editing })
  }

  return (
    <div className="rounded-lg border border-border bg-card p-4 shadow-xs">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold tracking-tight text-foreground">Pulse checks</h3>
        {operatorMode && !form && (query.data?.checks.length ?? 0) > 0 && (
          <Button size="sm" variant="outline" onClick={() => openForm(null, false)}>
            Add check
          </Button>
        )}
      </div>
      <QueryState
        query={query}
        isEmpty={(data) => data.checks.length === 0 && (data.proposals ?? []).length === 0 && !form && !saved}
        empty={<EmptyState operatorMode={operatorMode} onAdd={() => openForm(null, false)} />}
      >
        {(data) => (
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
              A pulse check runs only after you approve its exact text. Editing a check stops it
              until you approve it again.
            </p>
            {saved && (
              <p role="status" className="text-sm text-emerald-700 dark:text-emerald-400">
                Saved. Approve it below to start it.
              </p>
            )}
            {!data.authority_available && (
              <p className="text-sm text-destructive">
                Approval is unavailable: this server has no signing authority bound.
              </p>
            )}
            {!operatorMode && (
              <p className="text-xs italic text-muted-foreground/80">
                Turn on operator mode (top-right) to add, edit or approve checks.
              </p>
            )}
            {operatorMode && form && (
              <PulseCheckForm
                key={form.draft?.name ?? 'new'}
                agentId={agentId}
                draft={form.draft}
                editing={form.editing}
                onDone={(didSave) => {
                  setSaved(didSave)
                  setForm(null)
                }}
              />
            )}
            {(data.proposals ?? []).length > 0 && (
              <div className="space-y-2">
                <p className="text-xs font-medium text-muted-foreground">Proposed by the agent</p>
                <ul className="space-y-2">
                  {(data.proposals ?? []).map((proposal) =>
                    operatorMode ? (
                      <ProposalRow
                        key={proposal.name}
                        agentId={agentId}
                        proposal={proposal}
                        onReview={(draft) => openForm(draft, false)}
                      />
                    ) : (
                      <li key={proposal.name} className="text-sm text-muted-foreground">
                        {proposal.name}: {proposal.action}
                      </li>
                    ),
                  )}
                </ul>
              </div>
            )}
            <ul className="space-y-2">
              {data.checks.map((check) => (
                <CheckRow
                  key={check.name}
                  agentId={agentId}
                  check={check}
                  operatorMode={operatorMode}
                  authorityAvailable={data.authority_available}
                  onEdit={(draft) => openForm(draft, true)}
                />
              ))}
            </ul>
          </div>
        )}
      </QueryState>
    </div>
  )
}
