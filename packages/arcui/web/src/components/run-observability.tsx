// Agent spawn history for the ArcRun page — the sub-agents a run's agent has
// delegated to or spawned OVER TIME, not a fan-out of the one selected run.
// spawn_events carry no run_id, so this is deliberately agent-level: it answers
// "what has this agent branched off across its life", never "this run spawned N".
// Per-step tool/llm detail lives in the run drawer; LLM cost/usage on ArcLLM.
//
// The panel hides itself unless the agent actually has spawn history — the common
// case is none, and an empty box reads as broken. When there is history it stays
// collapsed behind a plain-language summary that a reader opens on demand, groups
// children by mechanism (delegate vs spawn) instead of a wall of identical rows,
// resolves DIDs to roster agent names, and lets a click focus a child's runs.
import { useState } from 'react'
import { ChevronRight, Copy, Check } from 'lucide-react'
import type { SpawnNode } from '@/lib/types'
import { useRoster, useSpawnTree } from '@/lib/queries'
import { shortId } from '@/lib/format'
import { cn } from '@/lib/utils'

/** Total descendants below a node — the agent's cumulative child count. */
function countDescendants(node: SpawnNode): number {
  return node.children.reduce((n, c) => n + 1 + countDescendants(c), 0)
}

/** A spawn DID shown as its roster name where known, else a short DID. */
function resolveName(did: string, nameByDid: Map<string, string>): string {
  return nameByDid.get(did) ?? shortId(did, 20)
}

type SpawnKind = 'delegate' | 'spawn' | 'other'

/** Mechanism a child was created by, read from its DID (did:arc:<kind>:child). */
function spawnKind(node: SpawnNode): SpawnKind {
  const seg = node.did.split(':')
  if (seg[0] === 'did' && seg[1] === 'arc') {
    if (seg[2] === 'delegate') return 'delegate'
    if (seg[2] === 'spawn') return 'spawn'
  }
  return 'other'
}

const KIND_VERB: Record<SpawnKind, string> = {
  delegate: 'delegated',
  spawn: 'spawned',
  other: 'branched',
}

function KindBadge({ kind }: { kind: SpawnKind }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-[0.06em]',
        kind === 'delegate'
          ? 'border border-primary/30 bg-primary/10 text-foreground'
          : 'border border-border bg-muted/40 text-muted-foreground',
      )}
    >
      {kind}
    </span>
  )
}

/** One child spawn — mechanism, name, outcome, and how much branched below it.
 *  The name is a button that focuses this child's runs in the Activity list; the
 *  DID is copyable for children that never surface as their own runs. */
function ChildRow({
  node,
  nameByDid,
  onFocus,
}: {
  node: SpawnNode
  nameByDid: Map<string, string>
  onFocus?: (child: { did: string; label: string }) => void
}) {
  const [copied, setCopied] = useState(false)
  const label = resolveName(node.did, nameByDid)
  const below = countDescendants(node)

  const copy = () => {
    void navigator.clipboard?.writeText(node.did)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1200)
  }

  return (
    <div className="flex items-center gap-2 py-1">
      <KindBadge kind={spawnKind(node)} />
      <button
        type="button"
        onClick={() => onFocus?.({ did: node.did, label })}
        title="Focus this child's runs in Activity"
        className="rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-foreground hover:border-primary/40 hover:text-primary"
      >
        {label}
      </button>
      {node.outcome && (
        <span className="text-[10px] uppercase tracking-[0.06em] text-muted-foreground/70">
          {node.outcome}
        </span>
      )}
      {below > 0 && (
        <span className="text-[10px] text-muted-foreground/60">
          +{below} below
        </span>
      )}
      <button
        type="button"
        onClick={copy}
        title="Copy DID"
        className="ml-auto text-muted-foreground/50 hover:text-foreground"
      >
        {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
      </button>
    </div>
  )
}

/** One mechanism group (all delegates, or all spawns), capped so a long history
 *  stays calm — the first rows show, the rest hide behind a "show N more". */
function GroupSection({
  kind,
  nodes,
  nameByDid,
  onFocus,
}: {
  kind: SpawnKind
  nodes: SpawnNode[]
  nameByDid: Map<string, string>
  onFocus?: (child: { did: string; label: string }) => void
}) {
  const [showAll, setShowAll] = useState(false)
  const cap = 8
  const visible = showAll ? nodes : nodes.slice(0, cap)

  return (
    <div className="space-y-0.5">
      <div className="flex items-center gap-2 pb-1 text-[11px] text-muted-foreground">
        <span className="font-semibold text-foreground">{nodes.length}</span>
        <span>{KIND_VERB[kind]}</span>
      </div>
      {visible.map((n) => (
        <ChildRow key={n.did} node={n} nameByDid={nameByDid} onFocus={onFocus} />
      ))}
      {nodes.length > cap && (
        <button
          type="button"
          onClick={() => setShowAll((s) => !s)}
          className="pt-1 text-[11px] font-medium text-primary hover:underline"
        >
          {showAll ? 'Show fewer' : `Show ${nodes.length - cap} more`}
        </button>
      )}
    </div>
  )
}

/** The agent's spawn/delegation history (UC-2) — NOT this run's fan-out. Renders
 *  nothing unless the agent actually has children. */
export function SpawnLineage({
  root,
  onFocusChild,
}: {
  root: string | null
  onFocusChild?: (child: { did: string; label: string }) => void
}) {
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

  const agent = resolveName(tree.did, nameByDid)
  const total = countDescendants(tree)

  // Group the agent's direct children by how they were created. Order delegate
  // first (the deliberate hand-offs), then spawns, then anything else.
  const order: SpawnKind[] = ['delegate', 'spawn', 'other']
  const byKind = new Map<SpawnKind, SpawnNode[]>()
  for (const child of tree.children) {
    const k = spawnKind(child)
    const bucket = byKind.get(k) ?? []
    bucket.push(child)
    byKind.set(k, bucket)
  }
  const groups = order
    .map((kind) => ({ kind, nodes: byKind.get(kind) ?? [] }))
    .filter((g) => g.nodes.length > 0)

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
          Agent spawn history
        </span>
        {groups.map((g) => (
          <span
            key={g.kind}
            className="rounded border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] font-medium text-foreground"
          >
            {g.nodes.length} {KIND_VERB[g.kind]}
          </span>
        ))}
      </button>
      <p className="mt-1.5 pl-[22px] text-[11px] leading-snug text-muted-foreground/80">
        Sub-agents <span className="text-foreground">{agent}</span> has spawned over time
        {total > 0 && ` — ${total} in all`}. Not this run&apos;s activity.
      </p>
      {open && (
        <div className="mt-3 space-y-4 rounded-lg border border-border bg-card p-4">
          {groups.map((g) => (
            <GroupSection
              key={g.kind}
              kind={g.kind}
              nodes={g.nodes}
              nameByDid={nameByDid}
              onFocus={onFocusChild}
            />
          ))}
        </div>
      )}
    </div>
  )
}
