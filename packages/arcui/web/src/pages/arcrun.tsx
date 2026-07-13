import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'
import { Workflow, Users } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { StatCard } from '@/components/stat-card'
import { DataTable } from '@/components/data-table'
import { StatusText } from '@/components/status-badge'
import { RunDetailDrawer } from '@/components/run-detail-drawer'
import { SpawnLineage } from '@/components/run-observability'
import { LoadingRows } from '@/components/states'
import { useRoster, useRuns } from '@/lib/queries'
import { fmtLatency, fmtNumber, relativeTime, shortId } from '@/lib/format'
import type { RunSummary } from '@/lib/types'

// The run spool carries the actor DID; the roster knows the human-facing name.
function resolveAgent(r: RunSummary, nameByDid: Map<string, string>): string {
  return (r.actor_did && nameByDid.get(r.actor_did)) || nameByDid.get(r.agent) || r.agent
}

export function ArcRunPage() {
  const { data, isLoading } = useRuns()
  const roster = useRoster()

  // Resolve actor DID → friendly agent name (the run spool carries the DID;
  // the roster knows the human-facing name).
  const nameByDid = useMemo(() => {
    const m = new Map<string, string>()
    for (const a of roster.data?.agents ?? []) {
      if (a.did) m.set(a.did, a.display_name || a.name || a.agent_id || a.did)
    }
    return m
  }, [roster.data])

  const columns = useMemo<ColumnDef<RunSummary, unknown>[]>(
    () => [
      {
        id: 'agent',
        header: 'Agent',
        accessorFn: (r) => resolveAgent(r, nameByDid),
        cell: (c) => <span className="font-medium text-xs text-foreground">{c.getValue() as string}</span>,
      },
      {
        accessorKey: 'run_id',
        header: 'Run',
        cell: (c) => (
          <span className="inline-flex items-center rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-primary">
            {shortId(c.getValue() as string, 14)}
          </span>
        ),
      },
      {
        accessorKey: 'status',
        header: 'Status',
        cell: (c) => <StatusText value={c.getValue() as string} />,
      },
      {
        accessorKey: 'turns',
        header: 'Turns',
        cell: (c) => <span className="font-mono text-xs tabular-nums text-muted-foreground">{fmtNumber(c.getValue() as number)}</span>,
      },
      {
        accessorKey: 'tool_calls',
        header: 'Tools',
        cell: (c) => <span className="font-mono text-xs tabular-nums text-muted-foreground">{fmtNumber(c.getValue() as number)}</span>,
      },
      {
        accessorKey: 'duration_ms',
        header: 'Duration',
        cell: (c) => <span className="font-mono text-xs tabular-nums text-muted-foreground">{fmtLatency(c.getValue() as number | null)}</span>,
      },
      {
        accessorKey: 'started_at',
        header: 'Started',
        cell: (c) => <span className="whitespace-nowrap text-xs text-muted-foreground">{relativeTime(c.getValue() as string)}</span>,
      },
    ],
    [nameByDid],
  )

  const rows = useMemo<RunSummary[]>(() => data?.runs ?? [], [data])
  const [active, setActive] = useState<RunSummary | null>(null)
  const agentsWithRuns = new Set(rows.map((r) => resolveAgent(r, nameByDid))).size

  // Deep-link support: `/arcrun?run=<id>` (e.g. a task's run link) auto-opens
  // that run once the rows load. Applied during render — keyed on the param so
  // it fires once and never fights a manual selection (mirrors the drawer's
  // render-time state-sync pattern; no setState-in-effect).
  const [searchParams] = useSearchParams()
  const runParam = searchParams.get('run')
  const [appliedRun, setAppliedRun] = useState<string | null>(null)
  if (runParam && runParam !== appliedRun && rows.length > 0) {
    setAppliedRun(runParam)
    const match = rows.find((r) => r.run_id === runParam)
    if (match) setActive(match)
  }

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="ArcRun" description="Agentic-loop runs — one per user-question→final-response cycle. Click a run for its step-by-step timeline." />
      <div className="flex-1 space-y-5 overflow-auto p-6">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-2">
          <StatCard label="Runs" value={rows.length} icon={<Workflow className="size-4" />} />
          <StatCard label="Agents with runs" value={agentsWithRuns} icon={<Users className="size-4" />} />
        </div>

        <section className="space-y-2">
          <h3 className="text-sm font-semibold text-foreground">Runs</h3>
          {isLoading ? (
            <LoadingRows />
          ) : (
            <DataTable
              columns={columns}
              data={rows}
              searchable
              searchPlaceholder="Search runs…"
              onRowClick={setActive}
              isRowActive={(r) => r.run_id === active?.run_id}
              emptyTitle="No runs recorded"
              emptyDescription="Each user-question→final-response cycle appears here as agents execute."
            />
          )}
        </section>

        <section className="space-y-2">
          <h3 className="text-sm font-semibold text-foreground">Spawn lineage</h3>
          <div className="rounded-lg border border-border bg-card p-4 shadow-xs">
            <SpawnLineage root={null} />
          </div>
        </section>
      </div>

      <RunDetailDrawer
        run={active}
        open={!!active}
        onOpenChange={(o) => !o && setActive(null)}
      />
    </div>
  )
}
