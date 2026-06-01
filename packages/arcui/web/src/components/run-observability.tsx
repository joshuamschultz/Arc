// SPEC-028 FR-4 — read-only tool/code timeline, spawn lineage tree, and
// per-identity LLM cost. All data comes from React Query (pull, no polling);
// these components only render what the Observe plane already persisted.
import type { SpawnNode, TimelineEntry } from '@/lib/types'
import { useIdentityCost, useRunTimeline, useSpawnTree } from '@/lib/queries'
import { fmtCost, fmtLatency, fmtNumber, shortId } from '@/lib/format'

const CODE_EXEC_TOOLS = new Set(['execute_python', 'execute'])

function TimelineRow({ entry }: { entry: TimelineEntry }) {
  if (entry.kind === 'tool_event') {
    const isCode = entry.tool_name ? CODE_EXEC_TOOLS.has(entry.tool_name) : false
    return (
      <div className="flex items-center gap-2 py-1 text-xs">
        <span className="font-mono text-primary">{isCode ? 'code' : 'tool'}</span>
        <span className="font-mono text-foreground">{entry.tool_name ?? '—'}</span>
        <span className="text-muted-foreground">{entry.phase}</span>
        {entry.outcome === 'error' && <span className="text-destructive">error</span>}
        {entry.latency_ms != null && (
          <span className="tabular-nums text-muted-foreground">{fmtLatency(entry.latency_ms)}</span>
        )}
      </div>
    )
  }
  if (entry.kind === 'llm_call') {
    return (
      <div className="flex items-center gap-2 py-1 text-xs">
        <span className="font-mono text-chart-2">llm</span>
        <span className="font-mono text-foreground">{entry.model ?? '—'}</span>
        {entry.cost_usd != null && (
          <span className="tabular-nums text-muted-foreground">{fmtCost(entry.cost_usd)}</span>
        )}
      </div>
    )
  }
  return (
    <div className="flex items-center gap-2 py-1 text-xs text-muted-foreground">
      <span className="font-mono">run</span>
      <span>{entry.name}</span>
    </div>
  )
}

/** Ordered tool/code/llm/run events for one run (UC-1/UC-4). */
export function RunTimeline({ runId }: { runId: string | null }) {
  const { data, isLoading } = useRunTimeline(runId)
  if (!runId) return null
  if (isLoading) return <p className="text-xs text-muted-foreground">Loading timeline…</p>
  const entries = data?.timeline ?? []
  if (!entries.length) return <p className="text-xs text-muted-foreground">No tool/code events for this run.</p>
  return (
    <div className="divide-y divide-border/50">
      {entries.map((e, i) => (
        <TimelineRow key={`${e.kind}-${i}`} entry={e} />
      ))}
    </div>
  )
}

function SpawnTreeNode({ node }: { node: SpawnNode }) {
  const label = `${node.role ?? 'child'} · ${shortId(node.did, 16)}`
  if (!node.children.length) {
    return <div className="py-0.5 font-mono text-xs text-foreground">{label}</div>
  }
  return (
    <details open className="py-0.5">
      <summary className="cursor-pointer font-mono text-xs text-foreground">{label}</summary>
      <div className="ml-4 border-l border-border/50 pl-3">
        {node.children.map((c) => (
          <SpawnTreeNode key={c.did} node={c} />
        ))}
      </div>
    </details>
  )
}

/** Parent→child lineage tree (UC-2), depth-bounded by the spawn engine. */
export function SpawnLineage({ root }: { root: string | null }) {
  const { data, isLoading } = useSpawnTree(root)
  if (isLoading) return <p className="text-xs text-muted-foreground">Loading lineage…</p>
  const tree = data?.tree
  if (!tree || !tree.did) return <p className="text-xs text-muted-foreground">No spawned agents recorded.</p>
  return <SpawnTreeNode node={tree} />
}

/** Per-identity LLM cost — parent vs each child, separated (UC-3). */
export function IdentityCostTable({ window = '24h' }: { window?: string }) {
  const { data, isLoading } = useIdentityCost(window)
  if (isLoading) return <p className="text-xs text-muted-foreground">Loading cost breakdown…</p>
  const rows = data?.identities ?? []
  if (!rows.length) return <p className="text-xs text-muted-foreground">No LLM calls in this window.</p>
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-left text-muted-foreground">
          <th className="py-1 font-medium">Identity</th>
          <th className="py-1 text-right font-medium">Calls</th>
          <th className="py-1 text-right font-medium">Tokens</th>
          <th className="py-1 text-right font-medium">Cost</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.identity} className="border-t border-border/50">
            <td className="py-1 font-mono text-foreground">{r.identity}</td>
            <td className="py-1 text-right tabular-nums text-muted-foreground">{fmtNumber(r.request_count)}</td>
            <td className="py-1 text-right tabular-nums text-muted-foreground">{fmtNumber(r.total_tokens)}</td>
            <td className="py-1 text-right tabular-nums text-foreground">{fmtCost(r.total_cost)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
