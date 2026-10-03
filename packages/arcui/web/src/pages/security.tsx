import { useMemo, useState, type ReactNode } from 'react'
import type { ColumnDef } from '@tanstack/react-table'
import { Ban, Check, Circle, ScrollText } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { FieldHelp } from '@/components/help'
import { DataTable } from '@/components/data-table'
import { FilterPills } from '@/components/filter-pills'
import { EventDrawer } from '@/components/event-drawer'
import { AgentIdentity } from '@/components/AgentIdentity'
import { SeverityBadge } from '@/components/status-badge'
import { QueryState, EmptyState } from '@/components/states'
import {
  AuditLinkChips,
  AuditVerdict,
  LedgerHash,
  LedgerSummary,
  SignedMark,
} from '@/components/audit/ledger'
import {
  auditField,
  causalChain,
  initiatorOf,
  isSigned,
  isVerified,
  signedByLabel,
  verificationOf,
  type AuditFilters,
} from '@/components/audit/ledger-utils'
import { CausalFilterBar } from '@/components/audit/causal-filters'
import { Button } from '@/components/ui/button'
import { TraceDrawer } from '@/components/trace-drawer'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useReverifyAudit, useTeamAudit } from '@/lib/queries'
import { relativeTime, fmtTime, shortId } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { AgentIdentityShape, AuditEvent } from '@/lib/types'

// Values drive the (server) query; labels read in operator language.
const FILTERS = [
  { value: 'all', label: 'All events' },
  { value: 'control', label: 'Control plane' },
  { value: 'deny', label: 'Denials' },
]

// The acting DID (`actor_did`), with the older `agent_id` name as a fallback.
const agentOf = (e: AuditEvent) => auditField(e, 'actor_did', 'agent_id')
// The plain-language action sentence, resolved server-side (H-022); the raw
// dotted `action`/`event_type` is the fallback for an event predating it.
const actionOf = (e: AuditEvent) => auditField(e, 'action_label', 'action', 'event_type')
// The verdict/severity — a real severity level, else the server-resolved
// decision word, else the raw outcome for an event predating H-022.
const verdictOf = (e: AuditEvent) => auditField(e, 'severity', 'decision', 'outcome')

// The canonical {AgentIdentity} shape (H-007/H-022), joined server-side by
// DID; a bare `did` (no roster row) still renders — just with no friendly name.
function eventIdentity(e: AuditEvent): AgentIdentityShape {
  if (e.identity) return e.identity
  const did = agentOf(e) ?? ''
  return { did, host: 'unknown', platform: 'unknown', type: 'unknown', short_id: 'unknown', name: null }
}

const capitalize = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

/** Compact actor cell. A row with a causal chain reads as the initiator kind and
 *  id ("Agent · olivia"), with who signed it underneath ("signed by operator");
 *  an older row falls back to the shared H-007 identity renderer. */
function ActorCell({ event }: { event: AuditEvent }) {
  const initiator = initiatorOf(event)
  if (initiator) {
    const name = event.actor ?? event.identity?.name ?? shortId(initiator.id, 24)
    const signedBy = signedByLabel(event)
    return (
      <div className="leading-tight" title={initiator.id}>
        <div className="text-xs font-medium text-foreground">
          {initiator.kind} · {name}
        </div>
        {signedBy && <div className="text-[10px] text-muted-foreground">{signedBy}</div>}
      </div>
    )
  }
  const identity = eventIdentity(event)
  const fallback = identity.type && identity.type !== 'unknown' ? capitalize(identity.type) : undefined
  return <AgentIdentity identity={identity} fallbackName={fallback} size="sm" showAvatar={false} />
}

const buildColumns = (onOpenLlmCall: (traceId: string) => void): ColumnDef<AuditEvent, unknown>[] => [
  {
    accessorFn: (r) => auditField(r, 'ts', 'timestamp') ?? '',
    id: 'timestamp',
    header: 'Time',
    cell: (c) => {
      const e = c.row.original
      const ts = auditField(e, 'ts', 'timestamp')
      const seq = e.seq
      return (
        <div className="whitespace-nowrap leading-tight">
          <div className="text-xs text-foreground" title={fmtTime(ts)}>
            {ts ? relativeTime(ts) : '—'}
          </div>
          {seq != null && (
            <div className="mt-0.5 font-mono text-[10px] text-muted-foreground/70">#{String(seq)}</div>
          )}
        </div>
      )
    },
  },
  {
    accessorFn: (r) => actionOf(r) ?? '',
    id: 'event',
    header: 'Event',
    cell: (c) => {
      const e = c.row.original
      return (
        <div className="flex items-center gap-2">
          <SignedMark event={e} />
          <div className="leading-tight">
            <div className="text-xs font-medium text-foreground">{actionOf(e) ?? '—'}</div>
            {e.action && (
              <div className="font-mono text-[10px] text-muted-foreground/70">{String(e.action)}</div>
            )}
          </div>
        </div>
      )
    },
  },
  {
    accessorFn: (r) => eventIdentity(r).name ?? agentOf(r) ?? '',
    id: 'actor',
    header: 'Actor',
    cell: (c) => <ActorCell event={c.row.original} />,
  },
  {
    accessorFn: (r) => auditField(r, 'target_label', 'target') ?? '',
    id: 'target',
    header: 'Target',
    cell: (c) => {
      const label = auditField(c.row.original, 'target_label', 'target')
      return (
        <span className="text-xs text-foreground" title={auditField(c.row.original, 'target')}>
          {label ?? '—'}
        </span>
      )
    },
  },
  {
    accessorFn: (r) => auditField(r, 'run_id', 'tool_call_id', 'llm_call_id', 'workflow_run_id', 'connection_id') ?? '',
    id: 'origin',
    header: 'Origin',
    cell: (c) => <AuditLinkChips event={c.row.original} onOpenLlmCall={onOpenLlmCall} />,
  },
  {
    accessorFn: (r) => verdictOf(r) ?? '',
    id: 'severity',
    header: 'Outcome',
    cell: (c) => {
      const e = c.row.original
      return (
        <span title={auditField(e, 'reason')}>
          <AuditVerdict value={verdictOf(e)} />
        </span>
      )
    },
  },
  {
    accessorFn: (r) => auditField(r, 'event_hash') ?? '',
    id: 'ledger',
    header: 'Ledger',
    cell: (c) => <LedgerHash value={auditField(c.row.original, 'event_hash')} />,
  },
]

export function SecurityPage() {
  const [filter, setFilter] = useState('all')
  const [causal, setCausal] = useState<AuditFilters>({})
  const filters = useMemo<AuditFilters>(
    () => ({ ...causal, filter: filter === 'all' ? undefined : filter }),
    [causal, filter],
  )
  const query = useTeamAudit(filters, 100)
  const [active, setActive] = useState<AuditEvent | null>(null)
  const [traceId, setTraceId] = useState<string | null>(null)
  const columns = useMemo(() => buildColumns(setTraceId), [])

  const events = useMemo(() => query.data?.events ?? [], [query.data])

  // Header counts come from the server's whole-ledger totals; the page is only
  // the fallback for a server that predates them.
  const summary = useMemo(() => {
    let verified = 0
    let denials = 0
    for (const e of events) {
      if (isVerified(e)) verified += 1
      const v = (verdictOf(e) ?? '').toLowerCase()
      if (v === 'deny' || v === 'denied' || v === 'blocked') denials += 1
    }
    const totals = query.data?.totals
    return {
      total: totals?.total ?? events.length,
      verified: totals?.verified ?? verified,
      broken: totals?.broken ?? 0,
      denials,
    }
  }, [events, query.data])

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Audit"
        description="The tamper-evident signed ledger — every control action, signature, and policy denial, in order."
      />
      <div className="flex-1 space-y-5 overflow-auto p-4 md:p-6">
        <LedgerSummary
          total={summary.total}
          verified={summary.verified}
          broken={summary.broken}
          denials={summary.denials}
        />

        <FilterPills value={filter} onChange={setFilter} options={FILTERS} />
        <CausalFilterBar value={causal} onApply={setCausal} />
        <FieldHelp helpKey="audit.event" route="security" />

        <QueryState query={query} isEmpty={() => events.length === 0} empty={
          <EmptyState
            icon={<ScrollText className="size-5" />}
            title="No audit events recorded"
            description="Signed control actions and policy denials appear here, newest first, as they occur."
          />
        }>
          {() => (
            <DataTable
              columns={columns}
              data={events}
              searchable
              searchPlaceholder="Search events, agents, hashes…"
              onRowClick={setActive}
              isRowActive={(r) => r === active}
              emptyTitle="No matching events"
            />
          )}
        </QueryState>
      </div>

      <EventDrawer
        open={!!active}
        onOpenChange={(o) => !o && setActive(null)}
        title={active ? actionOf(active) ?? 'Audit event' : 'Audit event'}
        description={active ? relativeTime(auditField(active, 'ts', 'timestamp')) : undefined}
        payload={active ?? undefined}
      >
        {active && <AuditDetail event={active} onOpenLlmCall={setTraceId} />}
      </EventDrawer>
      <TraceDrawer
        trace={traceId ? { trace_id: traceId } : null}
        open={traceId !== null}
        onOpenChange={(o) => !o && setTraceId(null)}
      />
    </div>
  )
}

/** The verified state in plain words, with the seq where a chain broke, and — for
 *  an operator — the action that re-walks the ledger and refreshes the verdicts. */
function VerificationNote({
  event,
  signed,
  verified,
}: {
  event: AuditEvent
  signed: boolean
  verified: boolean
}) {
  const [operatorMode] = useOperatorMode()
  const reverify = useReverifyAudit()
  const state = verificationOf(event)
  const result = reverify.data
  return (
    <div className="space-y-1.5">
      <p className="text-[11px] leading-relaxed text-muted-foreground">
        {state.state === 'broken'
          ? `Chain broken at seq ${state.seq ?? 'unknown'}: a record after this point does not match its signature or hash.`
          : signStateSentence(signed, verified)}
      </p>
      {operatorMode && state.state !== 'verified' && (
        <div className="flex items-center gap-2">
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={reverify.isPending}
            onClick={() => reverify.mutate()}
          >
            Re-verify ledger
          </Button>
          {result && (
            <span className="text-[11px] text-muted-foreground">
              {result.verified} of {result.events} verified
              {result.broken > 0 ? `, ${result.broken} broken` : ''}
            </span>
          )}
          {reverify.isError && (
            <span className="text-[11px] text-status-error">Re-verify failed.</span>
          )}
        </div>
      )}
    </div>
  )
}

/** One plain sentence explaining the entry's signed/verified state. */
function signStateSentence(signed: boolean, verified: boolean): string {
  if (!signed) return 'This entry carries no signature, so its integrity cannot be checked.'
  if (verified) return 'This entry is cryptographically signed and verified against a trusted key.'
  return 'This entry is cryptographically signed but has not been verified against a trusted key yet.'
}

const POSITIVE_DECISIONS = new Set(['allow', 'allowed', 'applied', 'ok', 'success', 'pass', 'passed'])
const NEGATIVE_DECISIONS = new Set(['deny', 'denied', 'blocked', 'error', 'fail', 'failed'])

// The decision as a colored chip — green for allow, red for deny — so the verdict
// reads at a glance. (The shared StatusChip leaves `allow` uncolored, hence this.)
function DecisionChip({ value }: { value: string | undefined }) {
  if (!value) return null
  const v = value.toLowerCase()
  const positive = POSITIVE_DECISIONS.has(v)
  const negative = NEGATIVE_DECISIONS.has(v)
  const tone = positive
    ? 'bg-status-online/12 text-status-online'
    : negative
      ? 'bg-status-error/12 text-status-error'
      : 'bg-muted text-muted-foreground'
  const Icon = positive ? Check : negative ? Ban : Circle
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-[11px] font-semibold capitalize',
        tone,
      )}
    >
      <Icon className="size-3" strokeWidth={2.6} />
      {value.replace(/_/g, ' ')}
    </span>
  )
}

/** The verdict as a graded severity badge, or a plain allow/deny decision chip. */
function VerdictMark({ value }: { value: string | undefined }) {
  if (!value) return null
  if (['critical', 'high', 'medium', 'low'].includes(value.toLowerCase())) {
    return <SeverityBadge value={value} />
  }
  return <DecisionChip value={value} />
}

/**
 * The drawer's detail — a plain-language summary (what happened, who did it, the
 * target, the decision) above the technical fields, hash chain, and raw JSON.
 */
function AuditDetail({
  event,
  onOpenLlmCall,
}: {
  event: AuditEvent
  onOpenLlmCall: (traceId: string) => void
}) {
  const signed = isSigned(event)
  const verified = isVerified(event)
  const chain = causalChain(event)
  const verdict = verdictOf(event)
  const agent = agentOf(event)
  const target = auditField(event, 'target')
  const targetLabel = auditField(event, 'target_label', 'target')
  const reason = auditField(event, 'reason')

  return (
    <div className="space-y-4">
      {/* Plain-language summary — what this entry means, before the raw record. */}
      <div className="space-y-3 rounded-lg border border-border bg-muted/30 p-3.5">
        <div className="flex flex-wrap items-center gap-2">
          <SignedMark event={event} />
          <span className="text-sm font-semibold text-foreground">{actionOf(event)}</span>
          <VerdictMark value={verdict} />
        </div>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-xs">
          <Field label="Who">
            <ActorCell event={event} />
          </Field>
          {targetLabel && <Field label="Target">{targetLabel}</Field>}
        </dl>
        {reason && (
          <p className="text-[11px] leading-relaxed text-foreground">
            <span className="font-semibold">Reason: </span>
            {reason}
          </p>
        )}
        <VerificationNote event={event} signed={signed} verified={verified} />
      </div>

      {chain.length > 0 && (
        <section
          aria-label="Causal chain"
          className="space-y-2 rounded-lg border border-border bg-muted/20 p-3"
        >
          <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            Causal chain
          </div>
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-xs">
            {chain.map(({ label, value }) => (
              <Field key={label} label={label}>
                <span className="break-all font-mono">{value}</span>
              </Field>
            ))}
          </dl>
          <AuditLinkChips event={event} onOpenLlmCall={onOpenLlmCall} />
        </section>
      )}

      <RecalledCards event={event} />

      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-xs">
        <Field label="Time">{fmtTime(auditField(event, 'ts', 'timestamp'))}</Field>
        {agent && (
          <Field label="Agent DID">
            <span className="break-all font-mono text-foreground">{agent}</span>
          </Field>
        )}
        {target && (
          <Field label="Raw target">
            <span className="break-all font-mono text-foreground">{target}</span>
          </Field>
        )}
        {event.seq != null && <Field label="Sequence">#{String(event.seq)}</Field>}
      </dl>

      {signed && (
        <div className="space-y-2 rounded-lg border border-signed/25 bg-signed/[0.06] p-3">
          <div className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            Hash chain
          </div>
          <HashRow label="This event" value={auditField(event, 'event_hash')} />
          <HashRow label="Previous" value={auditField(event, 'prev_hash')} />
          <HashRow label="Signature" value={auditField(event, 'signature')} />
        </div>
      )}
    </div>
  )
}

/** The memory cards a recall surfaced, from a `memory.recall_attributed` event's
 *  `extra.cards` (each `"<kind>/<slug>"`), with the optional detected-moment
 *  `trigger`. Renders nothing for any other event. This is where an operator sees
 *  WHICH cards were injected, not merely that a recall happened. */
function RecalledCards({ event }: { event: AuditEvent }) {
  const extra = (event.extra ?? {}) as Record<string, unknown>
  const cards = Array.isArray(extra.cards) ? extra.cards.map(String) : []
  const trigger = typeof extra.trigger === 'string' ? extra.trigger : ''
  if (cards.length === 0) return null
  return (
    <div className="space-y-2 rounded-lg border border-border bg-muted/20 p-3">
      <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
        Recalled cards
        {trigger && (
          <span className="rounded-sm border border-border bg-muted/60 px-1.5 py-0.5 font-mono text-[10px] normal-case tracking-normal text-foreground">
            {trigger}
          </span>
        )}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {cards.map((c) => (
          <span
            key={c}
            className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-foreground"
          >
            {c}
          </span>
        ))}
      </div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-foreground">{children}</dd>
    </>
  )
}

function HashRow({ label, value }: { label: string; value: string | undefined }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-[11px] text-muted-foreground">{label}</span>
      <span className="break-all text-right font-mono text-[11px] text-signed">{value ?? '—'}</span>
    </div>
  )
}
