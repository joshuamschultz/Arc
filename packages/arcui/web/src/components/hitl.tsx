import { useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Check, X, ShieldCheck, Database, Send, Bug, Info, ArrowRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { apiPost, ApiError } from '@/lib/api'
import { initials } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useRoster, type PendingApproval } from '@/lib/queries'

/* ---------------------------------------------------------------------------
 * Human-in-the-loop components. The moments a business operator must see and
 * act on — approvals, the trifecta gate, signed marks, context the user should
 * know — as distinctive, plain-language inline cards. Token-driven, flat.
 * ------------------------------------------------------------------------- */

const LEG_META: Record<string, { label: string; icon: typeof Database }> = {
  private_data: { label: 'Private data', icon: Database },
  external_comms: { label: 'External comms', icon: Send },
  untrusted_input: { label: 'Untrusted input', icon: Bug },
}
const TRIFECTA_ORDER = ['private_data', 'external_comms', 'untrusted_input'] as const

function legLabel(key: string): string {
  return LEG_META[key]?.label ?? key.replace(/_/g, ' ')
}

/** One capability leg as a lit chip. Unknown keys degrade to the raw name. */
function LegChip({ leg }: { leg: string }) {
  const Icon = LEG_META[leg]?.icon ?? Bug
  return (
    <div className="flex flex-col items-center gap-1.5 rounded-lg border border-status-warning/50 bg-status-warning/10 px-2 py-2.5 text-center">
      <Icon className="size-4 text-status-warning" />
      <span className="text-[10px] font-semibold uppercase tracking-[0.04em] text-status-warning">
        {legLabel(leg)}
      </span>
    </div>
  )
}

/**
 * The three trifecta legs shown as explicit slots; the triggered ones light.
 * Used only when all three legs are present (the full lethal trifecta) — a
 * smaller forbidden combination renders just its matched legs (see below).
 */
export function TrifectaGate({ legs }: { legs: string[] }) {
  const lit = new Set(legs)
  return (
    <div className="grid grid-cols-3 gap-2">
      {TRIFECTA_ORDER.map((key) => {
        const on = lit.has(key)
        const Icon = LEG_META[key].icon
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
              {LEG_META[key].label}
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

/* The argument keys that name WHO/WHERE the call reaches — surfaced first and
 * emphasized so the operator sees the target before the payload. */
const TARGET_KEYS = new Set([
  'to',
  'recipient',
  'recipients',
  'address',
  'email',
  'to_email',
  'phone',
  'url',
  'channel',
  'chat_id',
  'target',
  'to_did',
  'path',
])

/** What this call does — the redacted argument preview, target keys first. */
function CallArguments({ args }: { args: Record<string, string> }) {
  const entries = Object.entries(args)
  if (entries.length === 0) return null
  entries.sort((a, b) => {
    const at = TARGET_KEYS.has(a[0]) ? 0 : 1
    const bt = TARGET_KEYS.has(b[0]) ? 0 : 1
    return at - bt
  })
  return (
    <div className="rounded-lg border border-border bg-muted/25">
      <div className="border-b border-border px-3 py-1.5 text-[10px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
        What this call does
      </div>
      <dl className="divide-y divide-border/60">
        {entries.map(([key, value]) => {
          const isTarget = TARGET_KEYS.has(key)
          return (
            <div key={key} className="flex gap-3 px-3 py-1.5">
              <dt
                className={cn(
                  'w-24 shrink-0 font-mono text-[11px]',
                  isTarget ? 'font-semibold text-foreground' : 'text-muted-foreground',
                )}
              >
                {key}
              </dt>
              <dd
                className={cn(
                  'min-w-0 flex-1 break-words font-mono text-[11px]',
                  isTarget ? 'text-foreground' : 'text-foreground/80',
                )}
                title={value}
              >
                {value || <span className="text-muted-foreground/60 italic">empty</span>}
              </dd>
            </div>
          )
        })}
      </dl>
    </div>
  )
}

/** Why the gate fired — which prior calls lit each leg, in order, with times. */
function Provenance({ trail }: { trail: NonNullable<PendingApproval['provenance']> }) {
  if (trail.length === 0) return null
  return (
    <div className="space-y-1.5">
      <div className="text-[10px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
        How this got here
      </div>
      <ol className="space-y-1">
        {trail.map((step, i) => {
          const legs = (step.legs ?? []).map(legLabel).join(' + ')
          const at = step.at ? step.at.slice(11, 19) : ''
          return (
            <li
              key={i}
              className="flex items-center gap-2 rounded-md border border-border/70 bg-card px-2.5 py-1.5 text-xs"
            >
              <ArrowRight className="size-3 shrink-0 text-muted-foreground/60" />
              <span className="rounded border border-border bg-muted/40 px-1 py-0.5 font-mono text-[10px] text-foreground">
                {step.tool || 'tool'}
              </span>
              {legs && <span className="text-status-warning">{legs}</span>}
              {step.args && (
                <span className="min-w-0 truncate text-muted-foreground" title={step.args}>
                  {step.args}
                </span>
              )}
              {at && <span className="ml-auto shrink-0 tabular-nums text-muted-foreground/70">{at}</span>}
            </li>
          )
        })}
      </ol>
    </div>
  )
}

/**
 * The flagship approval / HITL card. Shows who is asking, the exact call and
 * its target, why it was gated (with the leg provenance), and — in operator
 * mode — a clear Approve / Deny. Self-resolving against
 * `/api/approvals/:id/{approve,deny}`.
 */
export function ApprovalRequest({
  a,
  operatorMode,
}: {
  a: PendingApproval
  operatorMode: boolean
}) {
  const queryClient = useQueryClient()
  const roster = useRoster()
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

  const rosterAgent = roster.data?.agents.find((g) => g.did === a.agent_did)
  const who =
    rosterAgent?.display_name ||
    rosterAgent?.name ||
    a.agent_label ||
    a.agent_did.split(/[:/]/).pop() ||
    'agent'
  const age = a.created_at ? a.created_at.slice(11, 19) : ''

  // Gate semantics (arctrust GlobalLayer.forbidden_composition): the gate fires
  // when a configured forbidden CAPABILITY COMBINATION is fully present — the
  // canonical one is the full lethal trifecta (all three legs), but a smaller
  // combination can be configured, and the tier-policy path fires with no legs
  // at all. Label and explain exactly what tripped, never a blanket "trifecta".
  const legs = a.legs ?? []
  const isFullTrifecta = TRIFECTA_ORDER.every((leg) => legs.includes(leg))
  const gateLabel = isFullTrifecta
    ? 'Lethal trifecta'
    : legs.length > 0
      ? 'Forbidden combination'
      : 'Policy approval'
  const why = isFullTrifecta
    ? 'Private data, an external channel, and untrusted input would all combine in one session — the lethal trifecta. Approving signs and unlocks exactly this one call.'
    : legs.length > 0
      ? `These capabilities are blocked from combining without your sign-off: ${legs
          .map(legLabel)
          .join(', ')}. Approving signs and unlocks exactly this one call.`
      : `Your security tier requires your sign-off before ${who} runs this tool. Approving signs and unlocks exactly this one call.`

  return (
    <div className="overflow-hidden rounded-xl border border-signed/25 bg-card shadow-sm">
      <div className="flex items-center gap-2.5 border-b border-border bg-signed/5 px-4 py-2.5">
        <SignedSeal />
        <span className="text-sm font-semibold text-foreground">Approval required</span>
        <span className="ml-auto text-[10px] font-semibold uppercase tracking-[0.08em] text-status-warning">
          {gateLabel}
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

        {a.arguments && <CallArguments args={a.arguments} />}

        {isFullTrifecta ? (
          <TrifectaGate legs={legs} />
        ) : legs.length > 0 ? (
          <div className="flex flex-wrap gap-2">
            {legs.map((leg) => (
              <div key={leg} className="w-24">
                <LegChip leg={leg} />
              </div>
            ))}
          </div>
        ) : null}

        <div className="rounded-lg border border-status-warning/25 bg-status-warning/8 px-3 py-2.5">
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-[0.06em] text-status-warning">
            Why this is gated
          </div>
          <p className="text-xs leading-relaxed text-foreground/90">{why}</p>
        </div>

        {a.provenance && a.provenance.length > 0 && <Provenance trail={a.provenance} />}

        {a.session_id && (
          <div className="text-[11px] text-muted-foreground">
            Session{' '}
            <span className="font-mono text-foreground/80">{a.session_id}</span>
          </div>
        )}

        {operatorMode ? (
          <div className="flex items-center gap-2 border-t border-border pt-3">
            <Button size="sm" onClick={() => resolve('approve')} disabled={busy !== null}>
              <Check className="size-3.5" /> Approve &amp; sign
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="text-destructive hover:text-destructive"
              onClick={() => resolve('deny')}
              disabled={busy !== null}
            >
              <X className="size-3.5" /> Deny
            </Button>
            {busy && <span className="text-xs text-muted-foreground">Working…</span>}
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
