import type { ReactNode } from 'react'
import { Fingerprint, Link2, ScrollText, ShieldAlert } from 'lucide-react'
import { SignedSeal } from '@/components/hitl'
import { SeverityBadge } from '@/components/status-badge'
import { StatusChip } from '@/components/ai'
import { cn } from '@/lib/utils'
import { shortId } from '@/lib/format'
import type { AuditEvent } from '@/lib/types'

/* ---------------------------------------------------------------------------
 * Audit ledger primitives (REDESIGN §5 "Signed mark", §8 Audit). The Audit
 * screen is Arc's tamper-evident signed ledger, so the tan `--signed` seal and
 * hash chips live here — the one place they read as governance, not accent.
 * Token-driven, flat. Composed only from the shared primitives.
 * ------------------------------------------------------------------------- */

const SEVERITY_LEVELS = new Set(['critical', 'high', 'medium', 'low'])

/** Real signed-chain field, with the older generic name as a fallback. */
export function auditField(e: AuditEvent, primary: string, ...fallbacks: string[]): string | undefined {
  for (const key of [primary, ...fallbacks]) {
    const v = e[key]
    if (v != null && v !== '') return String(v)
  }
  return undefined
}

/** A row is signed once it carries an Ed25519 signature over its chain link. */
export function isSigned(e: AuditEvent): boolean {
  return typeof e.signature === 'string' && e.signature.length > 0
}

/** The chain link verified on ingest (`verified` arrives as 0/1). */
export function isVerified(e: AuditEvent): boolean {
  return Boolean(e.verified)
}

/** Plain-language actor role read off the acting DID — Operator / Agent / System. */
export function actorRole(did: string | undefined): string {
  if (!did) return '—'
  if (did.includes(':operator')) return 'Operator'
  if (did.includes(':agent:')) return 'Agent'
  if (did.includes(':ui')) return 'Operator'
  return 'System'
}

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
  denials,
}: {
  total: number
  verified: number
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
        <LedgerMetric icon={<ScrollText className="size-3.5" />} label="Events" value={total} />
        <LedgerMetric
          icon={<Link2 className="size-3.5 text-signed" />}
          label="Verified"
          value={verified}
          tone="signed"
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
}: {
  icon: ReactNode
  label: string
  value: number
  tone?: 'signed' | 'error'
}) {
  const valueTone =
    tone === 'signed' ? 'text-signed' : tone === 'error' ? 'text-status-error' : 'text-foreground'
  return (
    <div className="flex items-center gap-2">
      <span className="text-muted-foreground/70">{icon}</span>
      <div className="leading-none">
        <div className={cn('font-display text-base font-extrabold tabular-nums', valueTone)}>
          {value}
        </div>
        <div className="mt-0.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          {label}
        </div>
      </div>
    </div>
  )
}
