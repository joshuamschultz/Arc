import { useMemo, useState, type ReactNode } from 'react'
import type { ColumnDef } from '@tanstack/react-table'
import { Ban, Check, Circle, ScrollText } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { DataTable } from '@/components/data-table'
import { FilterPills } from '@/components/filter-pills'
import { EventDrawer } from '@/components/event-drawer'
import { SeverityBadge } from '@/components/status-badge'
import { QueryState, EmptyState } from '@/components/states'
import {
  AuditVerdict,
  LedgerHash,
  LedgerSummary,
  SignedMark,
} from '@/components/audit/ledger'
import { auditField, actorRole, isSigned, isVerified } from '@/components/audit/ledger-utils'
import { useTeamAudit, useRoster } from '@/lib/queries'
import { relativeTime, fmtTime, shortId } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { AuditEvent } from '@/lib/types'

// Values drive the (server) query; labels read in operator language.
const FILTERS = [
  { value: 'all', label: 'All events' },
  { value: 'control', label: 'Control plane' },
  { value: 'deny', label: 'Denials' },
]

// The acting DID (`actor_did`), with the older `agent_id` name as a fallback.
const agentOf = (e: AuditEvent) => auditField(e, 'actor_did', 'agent_id')
// The event name (`action`), with the older `event_type` name as a fallback.
const actionOf = (e: AuditEvent) => auditField(e, 'action', 'event_type')
// The verdict/severity — a real severity level, else the outcome verdict.
const verdictOf = (e: AuditEvent) => auditField(e, 'severity', 'decision', 'outcome')

const columns: ColumnDef<AuditEvent, unknown>[] = [
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
    cell: (c) => (
      <div className="flex items-center gap-2">
        <SignedMark event={c.row.original} />
        <span className="rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-foreground">
          {actionOf(c.row.original) ?? '—'}
        </span>
      </div>
    ),
  },
  {
    accessorFn: (r) => agentOf(r) ?? '',
    id: 'agent_id',
    header: 'Agent',
    cell: (c) => {
      const did = agentOf(c.row.original)
      return (
        <span className="font-mono text-xs text-muted-foreground" title={did}>
          {did ? shortId(did, 20) : '—'}
        </span>
      )
    },
  },
  {
    accessorFn: (r) => auditField(r, 'actor') ?? actorRole(agentOf(r)),
    id: 'actor',
    header: 'Actor',
    cell: (c) => {
      const e = c.row.original
      const actor = auditField(e, 'actor') ?? actorRole(agentOf(e))
      return <span className="text-xs text-muted-foreground">{actor}</span>
    },
  },
  {
    accessorFn: (r) => verdictOf(r) ?? '',
    id: 'severity',
    header: 'Severity',
    cell: (c) => <AuditVerdict value={verdictOf(c.row.original)} />,
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
  const query = useTeamAudit(filter === 'all' ? undefined : filter, 100)
  const [active, setActive] = useState<AuditEvent | null>(null)

  const events = useMemo(() => query.data?.events ?? [], [query.data])

  const summary = useMemo(() => {
    let verified = 0
    let denials = 0
    for (const e of events) {
      if (isVerified(e)) verified += 1
      const v = (verdictOf(e) ?? '').toLowerCase()
      if (v === 'deny' || v === 'denied' || v === 'blocked') denials += 1
    }
    return { total: events.length, verified, denials }
  }, [events])

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Audit"
        description="The tamper-evident signed ledger — every control action, signature, and policy denial, in order."
      />
      <div className="flex-1 space-y-5 overflow-auto p-6">
        <LedgerSummary total={summary.total} verified={summary.verified} denials={summary.denials} />

        <FilterPills value={filter} onChange={setFilter} options={FILTERS} />

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
        {active && <AuditDetail event={active} />}
      </EventDrawer>
    </div>
  )
}

// Machine `action` -> plain-language sentence. Anything unmatched degrades to a
// readable Title-Case of the dotted name (module.bundle.verified -> "Module
// Bundle Verified"), so an unknown action is never shown raw.
const ACTION_LABELS: Record<string, string> = {
  'module.bundle.verified': 'Verified a module bundle',
  'module.bundle.loaded': 'Loaded a module bundle',
  'tool.call': 'Called a tool',
  'tool.result': 'Returned a tool result',
  'task.approve': 'Approved a task',
  'task.create': 'Created a task',
  'task.complete': 'Completed a task',
  'policy.allow': 'Policy allowed an action',
  'policy.deny': 'Policy denied an action',
  'memory.recall_attributed': 'Recalled memory cards',
  'policy.evaluate': 'Evaluated a policy',
  'skill.verified': 'Verified a skill',
  'session.start': 'Started a session',
  'run.start': 'Started a run',
  'run.complete': 'Completed a run',
  'memory.write': 'Wrote to memory',
}

function titleCase(action: string): string {
  return action
    .split(/[._-]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ')
}

/** Decode the machine `action` into a plain-language description of what happened. */
function describeAction(action: string | undefined): string {
  if (!action) return 'Recorded an event'
  return ACTION_LABELS[action] ?? titleCase(action)
}

// Turn the acting DID into a name a person recognizes: operator DIDs read as
// "Operator", agent/spawn DIDs resolve through the roster, else the plain role.
function resolveActor(did: string | undefined, nameByDid: Map<string, string>): string {
  if (!did) return 'Unknown'
  if (did.includes(':operator') || did.includes(':ui')) return 'Operator'
  const exact = nameByDid.get(did)
  if (exact) return exact
  // Spawn DIDs extend a base agent DID with a suffix — match on the prefix.
  for (const [base, name] of nameByDid) {
    if (did.startsWith(base)) return name
  }
  return actorRole(did)
}

/** A one-word target ("workpad") reads better title-cased; a path or id stays as-is. */
function plainTarget(target: string): string {
  return /^[a-z][a-z0-9_-]*$/.test(target) ? titleCase(target) : target
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
function AuditDetail({ event }: { event: AuditEvent }) {
  const roster = useRoster()
  const nameByDid = useMemo(() => {
    const m = new Map<string, string>()
    for (const a of roster.data?.agents ?? []) {
      if (a.did) m.set(a.did, a.display_name || a.name || a.agent_id || a.did)
    }
    return m
  }, [roster.data])

  const signed = isSigned(event)
  const verified = isVerified(event)
  const verdict = verdictOf(event)
  const agent = agentOf(event)
  const target = auditField(event, 'target')

  return (
    <div className="space-y-4">
      {/* Plain-language summary — what this entry means, before the raw record. */}
      <div className="space-y-3 rounded-lg border border-border bg-muted/30 p-3.5">
        <div className="flex flex-wrap items-center gap-2">
          <SignedMark event={event} />
          <span className="text-sm font-semibold text-foreground">
            {describeAction(actionOf(event))}
          </span>
          <VerdictMark value={verdict} />
        </div>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-xs">
          <Field label="Who">{resolveActor(agent, nameByDid)}</Field>
          {target && <Field label="Target">{plainTarget(target)}</Field>}
        </dl>
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          {signStateSentence(signed, verified)}
        </p>
      </div>

      <RecalledCards event={event} />

      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-xs">
        <Field label="Time">{fmtTime(auditField(event, 'ts', 'timestamp'))}</Field>
        {agent && (
          <Field label="Agent">
            <span className="break-all font-mono text-foreground">{agent}</span>
          </Field>
        )}
        {target && (
          <Field label="Target">
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
