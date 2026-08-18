import { useMemo, useState } from 'react'
import type { ColumnDef } from '@tanstack/react-table'
import { ScrollText } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { DataTable } from '@/components/data-table'
import { FilterPills } from '@/components/filter-pills'
import { EventDrawer } from '@/components/event-drawer'
import { StatusChip } from '@/components/ai'
import { SignedSeal } from '@/components/hitl'
import { SeverityBadge } from '@/components/status-badge'
import { QueryState, EmptyState } from '@/components/states'
import {
  auditField,
  actorRole,
  isSigned,
  isVerified,
  AuditVerdict,
  LedgerHash,
  LedgerSummary,
  SignedMark,
} from '@/components/audit/ledger'
import { useTeamAudit } from '@/lib/queries'
import { relativeTime, fmtTime, shortId } from '@/lib/format'
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
        <QueryState query={query} isEmpty={() => events.length === 0} empty={
          <div className="space-y-5">
            <LedgerSummary total={0} verified={0} denials={0} />
            <FilterPills value={filter} onChange={setFilter} options={FILTERS} />
            <EmptyState
              icon={<ScrollText className="size-5" />}
              title="No audit events recorded"
              description="Signed control actions and policy denials appear here, newest first, as they occur."
            />
          </div>
        }>
          {() => (
            <>
              <LedgerSummary
                total={summary.total}
                verified={summary.verified}
                denials={summary.denials}
              />
              <FilterPills value={filter} onChange={setFilter} options={FILTERS} />
              <DataTable
                columns={columns}
                data={events}
                searchable
                searchPlaceholder="Search events, agents, hashes…"
                onRowClick={setActive}
                isRowActive={(r) => r === active}
                emptyTitle="No matching events"
              />
            </>
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

/** The drawer's signed-chain detail — verdict, the acting agent, and the hash links. */
function AuditDetail({ event }: { event: AuditEvent }) {
  const signed = isSigned(event)
  const verified = isVerified(event)
  const verdict = verdictOf(event)
  const agent = agentOf(event)
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {signed && <SignedSeal />}
        <span className="text-sm font-semibold text-foreground">
          {signed ? (verified ? 'Signed & verified' : 'Signed — not verified') : 'Unsigned event'}
        </span>
        {verdict &&
          (['critical', 'high', 'medium', 'low'].includes(verdict.toLowerCase()) ? (
            <SeverityBadge value={verdict} />
          ) : (
            <StatusChip value={verdict} />
          ))}
      </div>

      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-xs">
        <Field label="Time">{fmtTime(auditField(event, 'ts', 'timestamp'))}</Field>
        {agent && (
          <Field label="Agent">
            <span className="break-all font-mono text-foreground">{agent}</span>
          </Field>
        )}
        {auditField(event, 'target') && (
          <Field label="Target">
            <span className="break-all font-mono text-foreground">{auditField(event, 'target')}</span>
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

function Field({ label, children }: { label: string; children: React.ReactNode }) {
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
