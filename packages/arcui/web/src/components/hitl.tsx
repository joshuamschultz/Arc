import { useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Check, X, ShieldCheck, Database, Send, Bug, Info } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { apiPost, ApiError } from '@/lib/api'
import { initials } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { PendingApproval } from '@/lib/queries'

/* ---------------------------------------------------------------------------
 * Human-in-the-loop components. The moments a business operator must see and
 * act on — approvals, the trifecta gate, signed marks, context the user should
 * know — as distinctive, plain-language inline cards. Token-driven, flat.
 * ------------------------------------------------------------------------- */

const LEGS = [
  { key: 'private_data', label: 'Private data', icon: Database },
  { key: 'external_comms', label: 'External comms', icon: Send },
  { key: 'untrusted_input', label: 'Untrusted input', icon: Bug },
] as const

/** The three trifecta legs shown as explicit slots; the triggered ones light. */
export function TrifectaGate({ legs }: { legs: string[] }) {
  const lit = new Set(legs)
  return (
    <div className="grid grid-cols-3 gap-2">
      {LEGS.map(({ key, label, icon: Icon }) => {
        const on = lit.has(key)
        return (
          <div
            key={key}
            className={cn(
              'flex flex-col items-center gap-1.5 rounded-lg border px-2 py-2.5 text-center transition-colors',
              on
                ? 'border-status-warning/50 bg-status-warning/10'
                : 'border-dashed border-border bg-muted/30',
            )}
          >
            <Icon
              className={cn('size-4', on ? 'text-status-warning' : 'text-muted-foreground/50')}
            />
            <span
              className={cn(
                'text-[10px] font-semibold uppercase tracking-[0.04em]',
                on ? 'text-status-warning' : 'text-muted-foreground/60',
              )}
            >
              {label}
            </span>
          </div>
        )
      })}
    </div>
  )
}

/** The tan seal — Arc's signed/verified mark. Used sparingly, only on signed. */
export function SignedSeal({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        'inline-grid size-5 place-items-center rounded-full bg-signed text-signed-foreground',
        className,
      )}
    >
      <ShieldCheck className="size-3" strokeWidth={2.6} />
    </span>
  )
}

/** A short banner of context the operator should see. */
export function ContextNote({
  tone = 'info',
  icon,
  children,
}: {
  tone?: 'info' | 'warning' | 'signed'
  icon?: ReactNode
  children: ReactNode
}) {
  const tones: Record<string, string> = {
    info: 'border-status-info/25 bg-status-info/8',
    warning: 'border-status-warning/30 bg-status-warning/10',
    signed: 'border-signed/30 bg-signed/10',
  }
  const iconTone: Record<string, string> = {
    info: 'text-status-info',
    warning: 'text-status-warning',
    signed: 'text-signed',
  }
  return (
    <div className={cn('flex items-start gap-2.5 rounded-lg border px-3 py-2.5', tones[tone])}>
      <span className={cn('mt-0.5 shrink-0 [&>svg]:size-3.5', iconTone[tone])}>
        {icon ?? <Info className="size-3.5" />}
      </span>
      <span className="text-xs leading-relaxed text-foreground/90">{children}</span>
    </div>
  )
}

/**
 * The flagship approval / HITL card. Shows who is asking, the exact call, the
 * trifecta gate visualized, a plain-language why, and (in operator mode) the
 * sign-off. Self-resolving against `/api/approvals/:id/{approve,deny}`.
 */
export function ApprovalRequest({
  a,
  operatorMode,
}: {
  a: PendingApproval
  operatorMode: boolean
}) {
  const queryClient = useQueryClient()
  const [busy, setBusy] = useState<'approve' | 'deny' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const resolve = async (decision: 'approve' | 'deny') => {
    setBusy(decision)
    setError(null)
    try {
      await apiPost(`/api/approvals/${encodeURIComponent(a.id)}/${decision}`)
      await queryClient.invalidateQueries({ queryKey: ['approvals'] })
    } catch (e) {
      setError(e instanceof ApiError ? e.message : `Could not ${decision}`)
      setBusy(null)
    }
  }

  const who = a.agent_label || a.agent_did.split('/').pop() || 'agent'
  const age = a.created_at ? a.created_at.slice(11, 19) : ''

  return (
    <div className="overflow-hidden rounded-xl border border-signed/25 bg-card shadow-sm">
      <div className="flex items-center gap-2.5 border-b border-border bg-signed/5 px-4 py-2.5">
        <SignedSeal />
        <span className="text-sm font-semibold text-foreground">Approval required</span>
        <span className="ml-auto text-[10px] font-semibold uppercase tracking-[0.08em] text-status-warning">
          Trifecta gate
        </span>
        {age && <span className="text-[11px] tabular-nums text-muted-foreground">{age}</span>}
      </div>
      <div className="space-y-3 p-4">
        <div className="flex items-center gap-2.5 text-sm">
          <span className="grid size-8 shrink-0 place-items-center rounded-md bg-primary/12 text-xs font-semibold text-foreground">
            {initials(who)}
          </span>
          <div className="min-w-0">
            <span className="font-semibold text-foreground">{who}</span>{' '}
            <span className="text-muted-foreground">wants to run</span>{' '}
            <span className="rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-foreground">
              {a.tool}
            </span>
          </div>
        </div>

        <TrifectaGate legs={a.legs} />

        <p className="text-xs leading-relaxed text-muted-foreground">
          Held because these must not combine unapproved — private data, reaching outside the box,
          and untrusted input together are the lethal trifecta. Approving signs and unlocks exactly
          this one call.
        </p>

        {operatorMode ? (
          <div className="flex items-center gap-2">
            <Button size="sm" onClick={() => resolve('approve')} disabled={busy !== null}>
              <Check className="size-3.5" /> Approve &amp; sign
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="text-destructive hover:text-destructive"
              onClick={() => resolve('deny')}
              disabled={busy !== null}
            >
              <X className="size-3.5" /> Deny
            </Button>
            {error && <span className="text-xs text-destructive">{error}</span>}
          </div>
        ) : (
          <ContextNote tone="warning">
            Turn on <span className="font-medium">operator controls</span> in the left rail to
            approve or deny.
          </ContextNote>
        )}
      </div>
    </div>
  )
}
