import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import { QueryState } from '@/components/states'
import { ApiError, apiGet, apiPost } from '@/lib/api'
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

type PulseListing = { checks: PulseCheck[]; authority_available: boolean }

const STATUS_LABEL: Record<PulseStatus, string> = {
  approved: 'Approved',
  unapproved: 'Unapproved',
  changes_pending: 'Changes pending approval',
}

const STATUS_STYLE: Record<PulseStatus, string> = {
  approved: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
  unapproved: 'border-border bg-muted/40 text-muted-foreground',
  changes_pending: 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400',
}

const pulsePath = (agentId: string) => `/api/agents/${encodeURIComponent(agentId)}/pulse`
const pulseKey = (agentId: string) => ['agents', agentId, 'pulse']

const errorText = (e: Error) => (e instanceof ApiError ? e.message : 'Could not approve this check')

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

function CheckRow({
  agentId,
  check,
  operatorMode,
  authorityAvailable,
}: {
  agentId: string
  check: PulseCheck
  operatorMode: boolean
  authorityAvailable: boolean
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
          <span className="text-xs text-muted-foreground">every {check.interval_minutes} min</span>
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
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </li>
  )
}

/**
 * "Pulse approvals" — a pulse check runs only from an operator-approved revision.
 * The operator reads each unapproved or edited check (with its diff against the
 * last approved definition) and approves exactly what is shown. Viewers see the
 * state read-only.
 */
export function PulsePanel({ agentId, operatorMode }: { agentId: string; operatorMode: boolean }) {
  const query = useQuery<PulseListing>({
    queryKey: pulseKey(agentId),
    queryFn: ({ signal }) => apiGet<PulseListing>(pulsePath(agentId), signal),
  })

  return (
    <div className="rounded-lg border border-border bg-card p-4 shadow-xs">
      <h3 className="mb-3 text-sm font-semibold tracking-tight text-foreground">Pulse approvals</h3>
      <QueryState
        query={query}
        isEmpty={(data) => data.checks.length === 0}
        empty={<p className="text-sm text-muted-foreground">No pulse checks in pulse.md.</p>}
      >
        {(data) => (
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
              A pulse check runs only after you approve its exact text. Editing pulse.md stops the
              check until you approve it again.
            </p>
            {!data.authority_available && (
              <p className="text-sm text-destructive">
                Approval is unavailable: this server has no signing authority bound.
              </p>
            )}
            {!operatorMode && (
              <p className="text-xs italic text-muted-foreground/80">
                Turn on operator mode (top-right) to approve checks.
              </p>
            )}
            <ul className="space-y-2">
              {data.checks.map((check) => (
                <CheckRow
                  key={check.name}
                  agentId={agentId}
                  check={check}
                  operatorMode={operatorMode}
                  authorityAvailable={data.authority_available}
                />
              ))}
            </ul>
          </div>
        )}
      </QueryState>
    </div>
  )
}
