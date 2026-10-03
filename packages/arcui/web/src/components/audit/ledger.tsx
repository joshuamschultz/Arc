import type { MouseEvent, ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Fingerprint, Link2, ScrollText, ShieldAlert, Unlink } from 'lucide-react'
import { SignedSeal } from '@/components/hitl'
import { SeverityBadge } from '@/components/status-badge'
import { StatusChip } from '@/components/ai'
import { cn } from '@/lib/utils'
import { shortId } from '@/lib/format'
import { auditLinks, isSigned, isVerified } from './ledger-utils'
import type { AuditEvent } from '@/lib/types'

/* ---------------------------------------------------------------------------
 * Audit ledger primitives (REDESIGN §5 "Signed mark", §8 Audit). The Audit
 * screen is Arc's tamper-evident signed ledger, so the tan `--signed` seal and
 * hash chips live here — the one place they read as governance, not accent.
 * Token-driven, flat. Composed only from the shared primitives.
 * ------------------------------------------------------------------------- */

const SEVERITY_LEVELS = new Set(['critical', 'high', 'medium', 'low'])

/**
 * The signed mark shown at the head of every ledger row. A verified link wears
 * the tan seal (felt, not read); a signature that did not verify is a genuine
 * tamper alert and shows loud.
 */
export function SignedMark({ event }: { event: AuditEvent }) {
  if (!isSigned(event)) {
    return <span className="inline-block size-5" aria-hidden />
  }
  if (isVerified(event)) return <SignedSeal />
  return (
    <span
      title="Signature did not verify"
      className="inline-grid size-5 place-items-center rounded-full bg-status-error/15 text-status-error"
    >
      <ShieldAlert className="size-3" strokeWidth={2.6} />
    </span>
  )
}

/** A hash from the chain, rendered as the tan mono chip — the ledger's fingerprint. */
export function LedgerHash({
  value,
  icon = <Fingerprint className="size-3 shrink-0 opacity-70" />,
}: {
  value: string | undefined
  icon?: ReactNode
}) {
  if (!value) return <span className="text-xs text-muted-foreground">—</span>
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-signed/25 bg-signed/8 px-1.5 py-0.5 text-signed">
      <span className="text-signed/70">{icon}</span>
      <span className="font-mono text-[11px]">{shortId(value, 12)}</span>
    </span>
  )
}

const CHIP_CLASS =
  'inline-flex items-center gap-1 rounded-md border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[10px] text-foreground hover:border-primary/40 hover:text-primary'

/**
 * Where an audit row came from: one chip per causal id. A run, tool call,
 * workflow run or connection navigates to its screen; an LLM call opens its
 * trace in place through `onOpenLlmCall`.
 */
export function AuditLinkChips({
  event,
  onOpenLlmCall,
}: {
  event: AuditEvent
  onOpenLlmCall?: (traceId: string) => void
}) {
  const links = auditLinks(event)
  if (links.length === 0) return <span className="text-xs text-muted-foreground">—</span>
  // A chip sits inside a clickable row; it must not also open the row's drawer.
  const stop = (e: MouseEvent<HTMLElement>) => e.stopPropagation()
  return (
    <div className="flex flex-wrap gap-1">
      {links.map((link) => {
        const name = `${link.label} ${link.id}`
        if (link.to) {
          return (
            <Link
              key={link.kind}
              to={link.to}
              onClick={stop}
              aria-label={`Open ${name}`}
              className={CHIP_CLASS}
            >
              {link.label} {shortId(link.id, 10)}
            </Link>
          )
        }
        return (
          <button
            key={link.kind}
            type="button"
            aria-label={`Open ${name}`}
            onClick={(e) => {
              stop(e)
              if (link.traceId) onOpenLlmCall?.(link.traceId)
            }}
            className={CHIP_CLASS}
          >
            {link.label} {shortId(link.id, 10)}
          </button>
        )
      })}
    </div>
  )
}

/**
 * The verdict/severity cell. A real severity level (critical…low) is graded by
 * `SeverityBadge`; anything else is an outcome verdict (allow / denied /
 * applied / error) and reads as a plain-language `StatusChip`.
 */
export function AuditVerdict({ value }: { value: string | undefined }) {
  if (!value) return <span className="text-xs text-muted-foreground">—</span>
  if (SEVERITY_LEVELS.has(value.toLowerCase())) return <SeverityBadge value={value} />
  return <StatusChip value={value} />
}

/**
 * The ledger summary strip that heads the screen — a quiet statement that this
 * is the signed, hash-chained record, plus the counts that matter at a glance.
 */
export function LedgerSummary({
  total,
  verified,
  broken,
  denials,
}: {
  total: number
  verified: number
  broken: number
  denials: number
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-3 rounded-lg border border-signed/25 bg-signed/[0.06] px-4 py-3">
      <div className="flex items-center gap-2.5">
        <SignedSeal className="size-6" />
        <div className="leading-tight">
          <div className="text-sm font-semibold text-foreground">Signed ledger</div>
          <div className="text-[11px] text-muted-foreground">
            Every action is signed and hash-chained — tamper-evident.
          </div>
        </div>
      </div>
      <div className="ml-auto flex items-center gap-5">
        <LedgerMetric
          icon={<ScrollText className="size-3.5" />}
          label="Events"
          value={total}
          testId="ledger-total"
        />
        <LedgerMetric
          icon={<Link2 className="size-3.5 text-signed" />}
          label="Verified"
          value={verified}
          tone="signed"
          testId="ledger-verified"
        />
        <LedgerMetric
          icon={<Unlink className="size-3.5 text-status-error" />}
          label="Broken"
          value={broken}
          tone={broken > 0 ? 'error' : undefined}
          testId="ledger-broken"
        />
        <LedgerMetric
          icon={<ShieldAlert className="size-3.5 text-status-error" />}
          label="Denials"
          value={denials}
          tone={denials > 0 ? 'error' : undefined}
        />
      </div>
    </div>
  )
}

function LedgerMetric({
  icon,
  label,
  value,
  tone,
  testId,
}: {
  icon: ReactNode
  label: string
  value: number
  tone?: 'signed' | 'error'
  testId?: string
}) {
  const valueTone =
    tone === 'signed' ? 'text-signed' : tone === 'error' ? 'text-status-error' : 'text-foreground'
  return (
    <div className="flex items-center gap-2">
      <span className="text-muted-foreground/70">{icon}</span>
      <div className="leading-none">
        <div
          data-testid={testId}
          className={cn('font-display text-base font-extrabold tabular-nums', valueTone)}
        >
          {value}
        </div>
        <div className="mt-0.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          {label}
        </div>
      </div>
    </div>
  )
}
