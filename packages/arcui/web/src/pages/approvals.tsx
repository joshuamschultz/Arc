import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { ShieldAlert, Check, X } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { OperatorModeToggle } from '@/components/operator-mode-toggle'
import { Button } from '@/components/ui/button'
import { QueryState, EmptyState } from '@/components/states'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useApprovals, type PendingApproval } from '@/lib/queries'
import { apiPost, ApiError } from '@/lib/api'
import { initials } from '@/lib/format'

// Human-readable gloss for the trifecta legs — the operator shouldn't need to
// know the internal token names to make the call.
const LEG_LABEL: Record<string, string> = {
  private_data: 'private data',
  external_comms: 'external comms',
  untrusted_input: 'untrusted input',
}

function legPhrase(legs: string[]): string {
  return legs.map((l) => LEG_LABEL[l] ?? l.replace(/_/g, ' ')).join(' + ')
}

function ApprovalCard({ a }: { a: PendingApproval }) {
  const queryClient = useQueryClient()
  const [operatorMode] = useOperatorMode()
  const [busy, setBusy] = useState<'approve' | 'deny' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const resolve = async (decision: 'approve' | 'deny') => {
    setBusy(decision)
    setError(null)
    try {
      await apiPost(`/api/approvals/${a.id}/${decision}`)
      await queryClient.invalidateQueries({ queryKey: ['approvals'] })
    } catch (e) {
      setError(e instanceof ApiError ? e.message : `Could not ${decision}`)
      setBusy(null)
    }
  }

  const who = a.agent_label || a.agent_did.split('/').pop() || 'agent'
  const created = a.created_at ? a.created_at.slice(11, 19) : ''

  return (
    <div className="rounded-lg border border-border bg-card p-4 shadow-xs">
      <div className="flex items-start gap-3">
        <span className="flex size-8 shrink-0 items-center justify-center rounded-md bg-primary/15 text-xs font-semibold text-foreground">
          {initials(who)}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="font-semibold text-foreground">{who}</span>
            <span className="text-sm text-muted-foreground">wants to run</span>
            <span className="rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-foreground">
              {a.tool}
            </span>
            {created && (
              <span className="ml-auto shrink-0 text-[10px] tabular-nums text-muted-foreground/70">
                {created}
              </span>
            )}
          </div>

          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            {a.legs.map((leg, i) => (
              <span key={leg} className="flex items-center gap-1.5">
                {i > 0 && <span className="text-xs text-amber-600/70 dark:text-amber-500/70">+</span>}
                <span className="rounded-sm border border-amber-500/30 bg-amber-500/10 px-1.5 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-400">
                  {LEG_LABEL[leg] ?? leg}
                </span>
              </span>
            ))}
          </div>
          <p className="mt-2 text-xs text-muted-foreground">
            This composition ({legPhrase(a.legs)}) is blocked pending your approval — the Lethal
            Trifecta. Approving unlocks exactly this one call.
          </p>

          {operatorMode ? (
            <div className="mt-3 flex items-center gap-2">
              <Button size="sm" onClick={() => resolve('approve')} disabled={busy !== null}>
                <Check className="size-3.5" /> Approve
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => resolve('deny')}
                disabled={busy !== null}
                className="text-destructive hover:text-destructive"
              >
                <X className="size-3.5" /> Deny
              </Button>
              {error && <span className="text-xs text-destructive">{error}</span>}
            </div>
          ) : (
            <p className="mt-3 text-xs italic text-muted-foreground/80">
              Enable operator mode to approve or deny.
            </p>
          )}
        </div>
      </div>
    </div>
  )
}

export function ApprovalsPage() {
  const approvals = useApprovals()

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Approvals"
        description="Mechanical operator approval for blocked agent actions."
        actions={<OperatorModeToggle />}
      />
      <div className="flex-1 overflow-auto p-6">
        <QueryState
          query={approvals}
          isEmpty={(data) => data.approvals.length === 0}
          empty={
            <EmptyState
              icon={<ShieldAlert className="size-7" />}
              title="No pending approvals"
              description="Blocked agent actions that need your sign-off will appear here."
            />
          }
        >
          {(data) => (
            <div className="mx-auto flex max-w-3xl flex-col gap-3">
              {data.approvals.map((a) => (
                <ApprovalCard key={a.id} a={a} />
              ))}
            </div>
          )}
        </QueryState>
      </div>
    </div>
  )
}
