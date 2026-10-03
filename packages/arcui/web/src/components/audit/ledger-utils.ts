import type { AuditCausal, AuditEvent } from '@/lib/types'

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

// --- Causality (item 20) ----------------------------------------------------
// Every audit row carries the causal chain it was written under: who initiated
// it and which run / call / workflow / connection it belongs to. The server
// flattens those onto the row as columns and also keeps the whole `causal` map.

const INITIATOR_LABELS: Record<string, string> = {
  agent: 'Agent',
  operator: 'Operator',
  scheduler: 'Scheduler',
  workflow: 'Workflow',
  ui_session: 'UI session',
  connector_probe: 'Connector probe',
  system: 'System',
}

/** One causal column, read from the flat column or the nested `causal` map. */
export function causalValue(e: AuditEvent, key: keyof AuditCausal): string | undefined {
  const v = e[key] ?? e.causal?.[key]
  return typeof v === 'string' && v !== '' ? v : undefined
}

const titleCase = (s: string) => s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, ' ')

/** Who caused this row: the initiator kind and its id. Undefined before causality. */
export function initiatorOf(e: AuditEvent): { kind: string; id: string } | undefined {
  const kind = causalValue(e, 'initiator')
  const id = causalValue(e, 'initiator_id')
  if (!kind || !id) return undefined
  return { kind: INITIATOR_LABELS[kind] ?? titleCase(kind), id }
}

/** Who signed the row's chain link — the operator key signs the UI chain. */
export function signedByLabel(e: AuditEvent): string | undefined {
  if (!e.signer) return undefined
  return String(e.chain ?? '').includes('arcui') ? 'signed by operator' : 'signed by agent'
}

export interface AuditLink {
  kind: 'run' | 'llm_call' | 'tool_call' | 'workflow' | 'connection'
  label: string
  id: string
  /** In-app route; absent for an LLM call, which opens in the trace drawer. */
  to?: string
  traceId?: string
}

/** Where each causal id leads, in the order an operator drills down. */
export function auditLinks(e: AuditEvent): AuditLink[] {
  const links: AuditLink[] = []
  const run = causalValue(e, 'run_id')
  const llm = causalValue(e, 'llm_call_id')
  const tool = causalValue(e, 'tool_call_id')
  const workflow = causalValue(e, 'workflow_run_id')
  const connection = causalValue(e, 'connection_id')
  if (run) links.push({ kind: 'run', label: 'Run', id: run, to: `/arcrun?run=${encodeURIComponent(run)}` })
  if (llm) links.push({ kind: 'llm_call', label: 'LLM call', id: llm, traceId: llm })
  // A tool call has no page of its own; it lives in its run's timeline.
  if (tool && run) {
    links.push({ kind: 'tool_call', label: 'Tool call', id: tool, to: `/arcrun?run=${encodeURIComponent(run)}` })
  }
  if (workflow) {
    links.push({
      kind: 'workflow',
      label: 'Workflow run',
      id: workflow,
      to: `/workflows?run=${encodeURIComponent(workflow)}`,
    })
  }
  if (connection) {
    links.push({
      kind: 'connection',
      label: 'Connection',
      id: connection,
      to: `/connections?connection=${encodeURIComponent(connection)}`,
    })
  }
  return links
}

export type Verification =
  | { state: 'verified' }
  | { state: 'unverified' }
  | { state: 'unsigned' }
  | { state: 'broken'; seq: number | string | undefined }

/** The chain-break marker row is the ingest's finding; its `seq` is where it broke. */
export const CHAIN_BROKEN_ACTION = 'audit.chain.broken'

export function verificationOf(e: AuditEvent): Verification {
  if (e.action === CHAIN_BROKEN_ACTION) return { state: 'broken', seq: e.seq }
  if (!isSigned(e)) return { state: 'unsigned' }
  return isVerified(e) ? { state: 'verified' } : { state: 'unverified' }
}

const CHAIN_FIELDS: [keyof AuditCausal, string][] = [
  ['initiator_id', 'Initiator'],
  ['on_behalf_of', 'On behalf of'],
  ['run_id', 'Run'],
  ['llm_call_id', 'LLM call'],
  ['tool_call_id', 'Tool call'],
  ['workflow_run_id', 'Workflow run'],
  ['node_id', 'Workflow node'],
  ['task_id', 'Task'],
  ['connection_id', 'Connection'],
]

/** The set causal ids as label/value pairs, for the row drawer. */
export function causalChain(e: AuditEvent): { label: string; value: string }[] {
  const out: { label: string; value: string }[] = []
  for (const [key, label] of CHAIN_FIELDS) {
    const value = causalValue(e, key)
    if (value) out.push({ label, value })
  }
  return out
}

export { auditQuery, type AuditFilters } from '@/lib/audit-query'
