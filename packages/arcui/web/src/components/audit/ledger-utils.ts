import type { AuditEvent } from '@/lib/types'

// Pure helpers for the Audit ledger. Kept out of ledger.tsx so that file only
// exports components (react-refresh).

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
