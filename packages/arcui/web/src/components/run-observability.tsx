// Spawn lineage tree for the ArcRun page — parent→child agent spawns within a
// run (a loop concern). Per-step tool/llm detail lives in the run drawer; LLM
// cost/usage lives on the ArcLLM page, not here.
import type { SpawnNode } from '@/lib/types'
import { useSpawnTree } from '@/lib/queries'
import { shortId } from '@/lib/format'

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
