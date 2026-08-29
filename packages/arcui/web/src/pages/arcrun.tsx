import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Search } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { RunRiver } from '@/components/run-river'
import { RunCoverflow } from '@/components/run-coverflow'
import { EmptyState, LoadingRows } from '@/components/states'
import { StatusChip } from '@/components/ai'
import { useRoster, useRuns } from '@/lib/queries'
import { initials, jobLabel, relativeTime, shortId } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { RunSummary } from '@/lib/types'

function resolveAgent(r: RunSummary, nameByDid: Map<string, string>): string {
  return (r.actor_did && nameByDid.get(r.actor_did)) || nameByDid.get(r.agent) || r.agent
}

export function ArcRunPage() {
  const { data, isLoading } = useRuns()
  const roster = useRoster()

  const nameByDid = useMemo(() => {
    const m = new Map<string, string>()
    for (const a of roster.data?.agents ?? []) {
      if (a.did) m.set(a.did, a.display_name || a.name || a.agent_id || a.did)
    }
    return m
  }, [roster.data])

  const colorByName = useMemo(() => {
    const m = new Map<string, string>()
    for (const a of roster.data?.agents ?? []) {
      const name = a.display_name || a.name || a.agent_id || ''
      if (name && typeof a.color === 'string') m.set(name, a.color)
    }
    return m
  }, [roster.data])

  const runs = useMemo<RunSummary[]>(() => data?.runs ?? [], [data])
  const [active, setActive] = useState<RunSummary | null>(null)
  const [q, setQ] = useState('')
  const [mode, setMode] = useState<'trace' | 'flip'>('trace')

  const [searchParams] = useSearchParams()
  const runParam = searchParams.get('run')

  // Initial selection: the deep-linked run, else the newest. Render-time set
  // converges (active becomes non-null, the guard is then false).
  if (!active && runs.length > 0) {
    const initial = runParam ? runs.find((r) => r.run_id === runParam) : runs[0]
    if (initial) setActive(initial)
  }

  const filtered = runs.filter((r) => {
    if (!q) return true
    const name = resolveAgent(r, nameByDid).toLowerCase()
    return name.includes(q.toLowerCase()) || r.run_id.toLowerCase().includes(q.toLowerCase())
  })

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Activity"
        description="Every run — pick one to see its signed action trace, step by step."
        actions={
          <div className="inline-flex gap-1 rounded-lg border border-border bg-card p-1">
            {(['trace', 'flip'] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(m)}
                className={cn(
                  'rounded-md px-3 py-1 text-xs font-semibold transition-colors',
                  mode === m
                    ? 'bg-primary/12 text-foreground'
                    : 'text-muted-foreground hover:text-foreground',
                )}
              >
                {m === 'trace' ? 'Trace' : 'Flip'}
              </button>
            ))}
          </div>
        }
      />
      {mode === 'flip' ? (
        <RunCoverflow
          runs={filtered}
          resolveName={(r) => resolveAgent(r, nameByDid)}
          colorFor={(n) => colorByName.get(n)}
          onOpen={(r) => {
            setActive(r)
            setMode('trace')
          }}
        />
      ) : (
      <div className="flex flex-1 overflow-hidden">
        <aside className="flex w-[300px] shrink-0 flex-col border-r border-border">
          <div className="border-b border-border p-2.5">
            <div className="flex items-center gap-2 rounded-md border border-border bg-card px-2.5 py-1.5 text-sm text-muted-foreground">
              <Search className="size-3.5" />
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search runs…"
                className="w-full bg-transparent text-foreground outline-none placeholder:text-muted-foreground"
              />
            </div>
          </div>
          <div className="flex-1 overflow-auto">
            {isLoading ? (
              <div className="p-3">
                <LoadingRows rows={6} />
              </div>
            ) : filtered.length === 0 ? (
              <div className="p-6">
                <EmptyState title="No runs recorded" description="Runs appear here as agents work." />
              </div>
            ) : (
              filtered.map((r) => {
                const name = resolveAgent(r, nameByDid)
                return (
                  <button
                    key={r.run_id}
                    type="button"
                    onClick={() => setActive(r)}
                    className={cn(
                      'flex w-full flex-col gap-1.5 border-b border-border px-4 py-3 text-left transition-colors hover:bg-muted/40',
                      active?.run_id === r.run_id && 'bg-primary/8',
                    )}
                  >
                    <div className="flex items-center gap-2">
                      <span
                        className="grid size-6 shrink-0 place-items-center rounded-md text-[10px] font-semibold text-white"
                        style={{ background: colorByName.get(name) || 'var(--primary)' }}
                      >
                        {initials(name)}
                      </span>
                      <div className="flex min-w-0 flex-col">
                        <span className="truncate text-sm font-semibold text-foreground">
                          {name}
                        </span>
                        {jobLabel(r.job) && (
                          <span
                            className="truncate text-[11px] leading-tight text-foreground/55"
                            title="A background job the agent ran on its own (not a person-driven run)"
                          >
                            {jobLabel(r.job)}
                          </span>
                        )}
                      </div>
                      <span className="ml-auto shrink-0 self-start text-[11px] tabular-nums text-muted-foreground">
                        {relativeTime(r.started_at)}
                      </span>
                    </div>
                    <div className="flex items-center gap-2 pl-8">
                      <StatusChip value={r.status} />
                      <span className="font-mono text-[11px] text-muted-foreground">
                        {shortId(r.run_id, 10)}
                      </span>
                    </div>
                  </button>
                )
              })
            )}
          </div>
        </aside>

        <main className="flex-1 overflow-auto">
          <RunRiver run={active} />
        </main>
      </div>
      )}
    </div>
  )
}
