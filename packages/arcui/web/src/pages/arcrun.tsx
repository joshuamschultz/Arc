import { useMemo, useState } from 'react'
import { useQueries } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { Workflow, Activity, Users } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { StatCard } from '@/components/stat-card'
import { DataTable } from '@/components/data-table'
import { RunReplayDrawer } from '@/components/run-replay-drawer'
import { EmptyState, LoadingRows } from '@/components/states'
import { apiGet } from '@/lib/api'
import { useRoster } from '@/lib/queries'
import { useLiveStore } from '@/store/live'
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

  const runEvents = useLiveStore((s) => s.runEvents)
  const [active, setActive] = useState<RunRow | null>(null)

  const agentsWithRuns = new Set(rows.map((r) => r.agent_id)).size
  const loading = roster.isLoading || sessionQueries.some((q) => q.isLoading)

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="ArcRun" description="Agentic-loop runs, traces, and live activity." />
      <div className="flex-1 space-y-5 overflow-auto p-6">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
          <StatCard label="Runs" value={rows.length} icon={<Workflow className="size-4" />} />
          <StatCard label="Agents with runs" value={agentsWithRuns} icon={<Users className="size-4" />} />
          <StatCard label="Live run events" value={runEvents.length} icon={<Activity className="size-4" />} />
        </div>

        <div className="grid grid-cols-1 gap-5 lg:grid-cols-[2fr_1fr]">
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

          <section className="space-y-2">
            <h3 className="text-sm font-semibold text-foreground">Live activity</h3>
            <div className="rounded-xl border border-border bg-card p-2 shadow-xs">
              {runEvents.length === 0 ? (
                <EmptyState title="No live run events" />
              ) : (
                <ul className="divide-y divide-border/60">
                  {runEvents.slice(0, 25).map((e, i) => (
                    <li key={i} className="flex items-center justify-between gap-2 px-2 py-1.5 text-xs">
                      <span className="truncate">
                        <span className="font-mono text-primary">{e.event_type}</span>
                        <span className="ml-2 text-muted-foreground">{e.agent_name || e.agent_id}</span>
                      </span>
                      <span className="shrink-0 text-muted-foreground">{relativeTime(e.timestamp)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </section>
        </div>
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
