// Spawn lineage tree for the ArcRun page — parent→child agent spawns rooted at
// the selected run's agent (a loop concern). Per-step tool/llm detail lives in
// the run drawer; LLM cost/usage lives on the ArcLLM page, not here.
//
// The panel hides itself entirely unless the run's agent actually spawned
// children — the common case is none, and an empty box reads as broken. When
// there is lineage it stays collapsed behind a "Spawned N" summary that a reader
// opens on demand, and spawn DIDs resolve to roster agent names where known.
import { useState } from 'react'
import { ChevronRight } from 'lucide-react'
import type { SpawnNode } from '@/lib/types'
import { useRoster, useSpawnTree } from '@/lib/queries'
import { shortId } from '@/lib/format'
import { cn } from '@/lib/utils'

/** Total descendants below a node — the "Spawned N children" count. */
function countDescendants(node: SpawnNode): number {
  return node.children.reduce((n, c) => n + 1 + countDescendants(c), 0)
}

/** A spawn DID shown as its roster name where known, else a short DID. */
function resolveName(did: string, nameByDid: Map<string, string>): string {
  return nameByDid.get(did) ?? shortId(did, 20)
}

function SpawnTreeNode({
  node,
  nameByDid,
}: {
  node: SpawnNode
  nameByDid: Map<string, string>
}) {
  const label = (
    <span className="inline-flex items-center gap-1.5 text-xs">
      <span className="text-muted-foreground">{node.role ?? 'child'}</span>
      <span className="inline-flex items-center rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-foreground">
        {resolveName(node.did, nameByDid)}
      </span>
      {node.outcome && (
        <span className="text-[10px] uppercase tracking-[0.06em] text-muted-foreground/70">
          {node.outcome}
        </span>
      )}
    </span>
  )
  if (!node.children.length) {
    return <div className="py-0.5">{label}</div>
  }
  return (
    <details open className="py-0.5">
      <summary className="cursor-pointer py-0.5">{label}</summary>
      <div className="ml-3 border-l border-border/50 pl-3">
        {node.children.map((c) => (
          <SpawnTreeNode key={c.did} node={c} nameByDid={nameByDid} />
        ))}
      </div>
    </details>
  )
}

/** Parent→child lineage for a run's agent (UC-2), depth-bounded by the spawn
 *  engine. Renders nothing unless real children exist. */
export function SpawnLineage({ root }: { root: string | null }) {
  const [open, setOpen] = useState(false)
  const { data } = useSpawnTree(root)
  const roster = useRoster()

  const tree = data?.tree
  // Nothing to show unless the agent actually spawned children — the panel
  // stays fully hidden rather than rendering an empty or single-node box.
  if (!tree || !tree.did || tree.children.length === 0) return null

  const nameByDid = new Map<string, string>()
  for (const a of roster.data?.agents ?? []) {
    if (a.did) nameByDid.set(a.did, a.display_name || a.name || a.agent_id || a.did)
  }
  const count = countDescendants(tree)

  return (
    <div className="border-t border-border p-6">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="group flex items-center gap-2 text-left"
      >
        <ChevronRight
          className={cn('size-3.5 text-muted-foreground transition-transform', open && 'rotate-90')}
        />
        <span className="text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground group-hover:text-foreground">
          Spawn lineage
        </span>
        <span className="rounded border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] font-medium text-foreground">
          Spawned {count} {count === 1 ? 'child' : 'children'}
        </span>
      </button>
      {open && (
        <div className="mt-3 rounded-lg border border-border bg-card p-4">
          <SpawnTreeNode node={tree} nameByDid={nameByDid} />
        </div>
      )}
    </div>
  )
}
