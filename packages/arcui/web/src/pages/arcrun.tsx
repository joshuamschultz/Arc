import { useMemo, useState } from 'react'
import { useQueries } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { Workflow, Users } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { StatCard } from '@/components/stat-card'
import { DataTable } from '@/components/data-table'
import { RunReplayDrawer } from '@/components/run-replay-drawer'
import { IdentityCostTable, RunTimeline, SpawnLineage } from '@/components/run-observability'
import { LoadingRows } from '@/components/states'
import { apiGet } from '@/lib/api'
import { useRoster } from '@/lib/queries'
import { fmtBytes, relativeTime, shortId } from '@/lib/format'
import type { SessionsListResponse } from '@/lib/types'

interface RunRow {
  agent_id: string
  sid: string
  size: number
  mtime: number
}

const columns: ColumnDef<RunRow, unknown>[] = [
  {
    accessorKey: 'agent_id',
    header: 'Agent',
    cell: (c) => <span className="font-mono text-xs text-foreground">{String(c.getValue())}</span>,
  },
  {
    accessorKey: 'sid',
    header: 'Run',
    cell: (c) => <span className="font-mono text-xs text-primary">{shortId(c.getValue() as string, 18)}</span>,
  },
  {
    accessorKey: 'size',
    header: 'Size',
    cell: (c) => <span className="font-mono text-xs tabular-nums text-muted-foreground">{fmtBytes(c.getValue() as number)}</span>,
  },
  {
    accessorKey: 'mtime',
    header: 'Updated',
    cell: (c) => <span className="whitespace-nowrap text-xs text-muted-foreground">{relativeTime(c.getValue() as number)}</span>,
  },
]

export function ArcRunPage() {
  const roster = useRoster()
  const agents = useMemo(
    () => (roster.data?.agents ?? []).filter((a) => !a.hidden && a.agent_id),
    [roster.data],
  )

  // Runs are per-agent sessions — fan out across the fleet and merge.
  const sessionQueries = useQueries({
    queries: agents.map((a) => ({
      queryKey: ['agent', a.agent_id, 'sessions'],
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        apiGet<SessionsListResponse>(`/api/agents/${a.agent_id}/sessions`, signal),
    })),
  })

  const rows = useMemo<RunRow[]>(() => {
    const out: RunRow[] = []
    sessionQueries.forEach((q, i) => {
      const agentId = agents[i]?.agent_id ?? ''
      for (const s of q.data?.sessions ?? []) {
        out.push({ agent_id: agentId, sid: s.sid, size: s.size, mtime: s.mtime })
      }
    })
    return out.sort((a, b) => b.mtime - a.mtime)
  }, [sessionQueries, agents])

  const [active, setActive] = useState<RunRow | null>(null)

  const agentsWithRuns = new Set(rows.map((r) => r.agent_id)).size
  const loading = roster.isLoading || sessionQueries.some((q) => q.isLoading)

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="ArcRun" description="Agentic-loop runs and traces." />
      <div className="flex-1 space-y-5 overflow-auto p-6">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-2">
          <StatCard label="Runs" value={rows.length} icon={<Workflow className="size-4" />} />
          <StatCard label="Agents with runs" value={agentsWithRuns} icon={<Users className="size-4" />} />
        </div>

        <section className="space-y-2">
          <h3 className="text-sm font-semibold text-foreground">Runs</h3>
          {loading ? (
            <LoadingRows />
          ) : (
            <DataTable
              columns={columns}
              data={rows}
              searchable
              searchPlaceholder="Search runs…"
              onRowClick={setActive}
              isRowActive={(r) => r.sid === active?.sid}
              emptyTitle="No runs recorded"
              emptyDescription="Agentic runs (sessions) appear here as agents execute."
            />
          )}
        </section>

        {/* SPEC-028 — tool/code timeline for the selected run (UC-1/UC-4). */}
        {active && (
          <section className="space-y-2">
            <h3 className="text-sm font-semibold text-foreground">
              Tool / code timeline — {shortId(active.sid, 18)}
            </h3>
            <RunTimeline runId={active.sid} />
          </section>
        )}

        {/* SPEC-028 — spawn lineage (UC-2) + per-identity cost (UC-3). */}
        <section className="grid gap-5 lg:grid-cols-2">
          <div className="space-y-2">
            <h3 className="text-sm font-semibold text-foreground">Spawn lineage</h3>
            <SpawnLineage root={null} />
          </div>
          <div className="space-y-2">
            <h3 className="text-sm font-semibold text-foreground">LLM cost by identity</h3>
            <IdentityCostTable window="24h" />
          </div>
        </section>
      </div>

      <RunReplayDrawer
        agentId={active?.agent_id ?? null}
        sid={active?.sid ?? null}
        open={!!active}
        onOpenChange={(o) => !o && setActive(null)}
      />
    </div>
  )
}
