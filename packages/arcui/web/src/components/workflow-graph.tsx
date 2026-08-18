import { memo, useCallback, useEffect, useMemo } from 'react'
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import dagre from 'dagre'
import { Bot, FileCode2, GitBranch, ShieldCheck, User, Wrench } from 'lucide-react'
import type { ComponentType } from 'react'
import { cn } from '@/lib/utils'
import { useRoster } from '@/lib/queries'
import type {
  Agent,
  WorkflowEdge as WfEdge,
  WorkflowNode as WfNode,
  WorkflowNodeKind,
  WorkflowNodeStatus,
} from '@/lib/types'

// SDD §8 / DESIGN.md Research Insights: dagre (~25KB gz) over elkjs
// (~433KB gz) — elkjs's cycle-breaking strategies are unneeded because
// declared bounded back-edges are already reversed for layout, and dagre's
// feedback-arc reversal handles them.
const DAGRE_NODE_WIDTH = 200
const DAGRE_NODE_HEIGHT = 96

export interface GraphNodeData extends Record<string, unknown> {
  label: string
  kind: string
  /** Resolved runner display name (agent that executes this node), or null for
   *  a gate (human step) and any node without an explicit runner. */
  runner?: string | null
  /** Kind-specific one-liner: the tool called, script run, gate decision,
   *  router mode, or agent skill/strategy. */
  detail?: string | null
  /** `loops → <target> (max N)` when the node declares a loop-back. */
  loop?: string | null
  status?: WorkflowNodeStatus
  iteration?: number | null
  maxIterations?: number | null
  hasError?: boolean
}

const STATUS_TONE: Record<string, string> = {
  pending: 'border-border bg-muted/30 text-muted-foreground',
  running: 'border-status-info/60 bg-status-info/10 text-status-info animate-pulse',
  waiting_gate: 'border-status-warning/60 bg-status-warning/10 text-status-warning',
  done: 'border-status-online/60 bg-status-online/10 text-status-online',
  failed: 'border-status-error/60 bg-status-error/10 text-status-error',
  skipped: 'border-border bg-muted/10 text-muted-foreground/50 opacity-60',
  looping: 'border-status-info/60 bg-status-info/10 text-status-info',
}

// Color-coded type tag per node kind (the user's ask: agent=emerald,
// gate=amber, router=blue decision, tool/script=graphite). Uses the shared
// status tokens so light/dark both read correctly.
const KIND_TONE: Record<string, string> = {
  agent: 'border-status-online/30 bg-status-online/15 text-status-online',
  gate: 'border-status-warning/30 bg-status-warning/15 text-status-warning',
  router: 'border-status-info/30 bg-status-info/15 text-status-info',
  tool: 'border-border bg-muted text-muted-foreground',
  script: 'border-border bg-muted text-muted-foreground',
}

const KIND_ICON: Record<string, ComponentType<{ className?: string }>> = {
  agent: Bot,
  gate: ShieldCheck,
  router: GitBranch,
  tool: Wrench,
  script: FileCode2,
}

// Memoized per React Flow's perf guidance — a custom node re-renders on every
// prop change otherwise, and with wide graphs that's the dominant cost.
const WorkflowGraphNode = memo(function WorkflowGraphNode({
  data,
  selected,
}: NodeProps & { data: GraphNodeData }) {
  const tone = STATUS_TONE[data.status ?? 'pending'] ?? STATUS_TONE.pending
  const kindTone = KIND_TONE[data.kind] ?? KIND_TONE.tool
  const KindIcon = KIND_ICON[data.kind] ?? Wrench
  return (
    <div
      className={cn(
        'min-w-[160px] max-w-[200px] rounded-lg border-2 px-3 py-2 text-xs shadow-xs transition-colors',
        tone,
        selected && 'ring-2 ring-ring ring-offset-1 ring-offset-background',
        data.hasError && 'border-destructive',
      )}
    >
      <Handle type="target" position={Position.Left} className="!size-2 !bg-muted-foreground" />
      <span
        className={cn(
          'inline-flex items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[9px] font-medium uppercase tracking-wide',
          kindTone,
        )}
      >
        <KindIcon className="size-2.5" />
        {data.kind}
      </span>
      <div className="mt-1 truncate font-semibold text-foreground">{data.label}</div>
      {data.runner ? (
        <div className="mt-1 flex items-center gap-1 text-[10px] text-muted-foreground">
          <User className="size-2.5 shrink-0" />
          <span className="truncate font-medium">{data.runner}</span>
        </div>
      ) : null}
      {data.detail ? (
        <div className="mt-0.5 truncate text-[10px] text-muted-foreground/80">{data.detail}</div>
      ) : null}
      {data.loop ? (
        <div className="mt-0.5 truncate text-[10px] text-muted-foreground/80">{data.loop}</div>
      ) : null}
      {data.status === 'looping' ? (
        <div className="mt-1 text-[10px]">
          looping {data.iteration ?? 0} of {data.maxIterations ?? '?'}
        </div>
      ) : data.status ? (
        <div className="mt-1 text-[10px] capitalize">{data.status.replace(/_/g, ' ')}</div>
      ) : null}
      <Handle type="source" position={Position.Right} className="!size-2 !bg-muted-foreground" />
    </div>
  )
})

/** Resolve a node's `agent` handle (e.g. `@sales`, or a DID) to a display name
 *  against the loaded roster. Returns the raw handle when nothing matches so a
 *  runner is always shown; null only when there is no handle at all. */
function resolveRunner(raw: unknown, agents: Agent[]): string | null {
  if (raw == null || String(raw).trim() === '') return null
  const handle = String(raw).replace(/^@/, '')
  const match = agents.find(
    (a) => a.agent_id === handle || a.name === handle || a.did === handle,
  )
  return match?.display_name || match?.name || match?.agent_id || handle
}

/** The kind-specific metadata line: what the node actually does. */
function nodeDetail(node: WfNode): string | null {
  const raw = node as Record<string, unknown>
  const text = (v: unknown) => (v == null || String(v).trim() === '' ? null : String(v).trim())
  switch (node.kind as WorkflowNodeKind) {
    case 'tool':
      return text(raw.tool) ? `calls ${String(raw.tool)}` : null
    case 'script':
      return text(raw.script) ? `runs ${String(raw.script)}` : null
    case 'gate':
      return text(raw.gate) ?? 'human approval'
    case 'router':
      return raw.mode === 'llm' ? 'routes by model' : 'routes by rules'
    case 'agent': {
      if (text(raw.skill)) return `skill: ${String(raw.skill)}`
      const strategy = Array.isArray(raw.strategy)
        ? raw.strategy.join(', ')
        : text(raw.strategy)
      if (strategy) return `strategy: ${strategy}`
      return text(raw.prompt) ? `prompt: ${String(raw.prompt)}` : null
    }
    default:
      return null
  }
}

/** `loops → <target> (max N)` for a declared loop-back, else null. */
function loopLabel(node: WfNode): string | null {
  if (!node.loop_back_to) return null
  const cap = node.max_iterations ? ` (max ${node.max_iterations})` : ''
  return `loops → ${node.loop_back_to}${cap}`
}

// Hoisted once at module scope — recreating this object inline on every
// render is the #1 React Flow remount bug (DESIGN.md §8 Research Insights).
const nodeTypes = { workflowNode: WorkflowGraphNode }

// Layout cache keyed by workflow id + version: a signed definition's graph
// shape is immutable, so re-layout only happens on a structural edit (a new
// draft version), never on a live-status tick.
const layoutCache = new Map<string, Array<{ id: string; x: number; y: number }>>()

function layoutPositions(cacheKey: string, nodes: WfNode[], edges: WfEdge[]) {
  const cached = layoutCache.get(cacheKey)
  if (cached) return cached
  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: 'LR', nodesep: 36, ranksep: 72 })
  g.setDefaultEdgeLabel(() => ({}))
  for (const n of nodes) g.setNode(n.id, { width: DAGRE_NODE_WIDTH, height: DAGRE_NODE_HEIGHT })
  for (const e of edges) g.setEdge(e.from, e.to)
  dagre.layout(g)
  const positions = nodes.map((n) => {
    const p = g.node(n.id)
    return { id: n.id, x: (p?.x ?? 0) - DAGRE_NODE_WIDTH / 2, y: (p?.y ?? 0) - DAGRE_NODE_HEIGHT / 2 }
  })
  layoutCache.set(cacheKey, positions)
  return positions
}

export interface NodeStatusUpdate {
  status: WorkflowNodeStatus
  iteration?: number | null
  max_iterations?: number | null
}

interface WorkflowGraphProps {
  workflowId: string
  version: number
  nodes: WfNode[]
  edges: WfEdge[]
  /** Drag from one node's right handle to another's left: the target now
   *  `needs` the source. Absent on a run view, where the graph is a record. */
  onConnectNodes?: (source: string, target: string) => void
  onDisconnectNodes?: (source: string, target: string) => void
  /** Per-node live/final status. Absent nodes render with no status pill. */
  nodeStatus?: Record<string, NodeStatusUpdate>
  /** node_id -> true for nodes a validation error is against (REQ-253). */
  errorNodeIds?: Set<string>
  onNodeClick?: (nodeId: string) => void
}

function WorkflowGraphInner({
  workflowId,
  version,
  nodes,
  edges,
  nodeStatus,
  errorNodeIds,
  onNodeClick,
  onConnectNodes,
  onDisconnectNodes,
}: WorkflowGraphProps) {
  const cacheKey = `${workflowId}:${version}`
  const positions = useMemo(() => layoutPositions(cacheKey, nodes, edges), [cacheKey, nodes, edges])
  const roster = useRoster()
  const agents = roster.data?.agents

  const initialNodes = useMemo<Node<GraphNodeData>[]>(
    () =>
      nodes.map((n) => {
        const pos = positions.find((p) => p.id === n.id)
        const runner =
          n.kind === 'gate' ? null : resolveRunner(n.agent, agents ?? []) ?? 'workflow owner'
        return {
          id: n.id,
          type: 'workflowNode',
          position: { x: pos?.x ?? 0, y: pos?.y ?? 0 },
          data: {
            label: n.id,
            kind: n.kind,
            runner,
            detail: nodeDetail(n),
            loop: loopLabel(n),
            status: nodeStatus?.[n.id]?.status,
            iteration: nodeStatus?.[n.id]?.iteration,
            maxIterations: nodeStatus?.[n.id]?.max_iterations,
            hasError: errorNodeIds?.has(n.id) ?? false,
          },
          sourcePosition: Position.Right,
          targetPosition: Position.Left,
        }
      }),
    // Deliberately excludes `nodeStatus` after mount — live ticks apply via
    // `updateNodeData` below (v12 perf guidance), never by remapping this
    // array. `agents` is included so runner names fill in once the roster
    // loads; a structural change (nodes/edges/cacheKey) also rebuilds it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [nodes, positions, cacheKey, errorNodeIds, agents],
  )
  const initialEdges = useMemo<Edge[]>(
    () =>
      edges.map((e) => ({
        id: `${e.from}->${e.to}`,
        source: e.from,
        target: e.to,
        markerEnd: { type: MarkerType.ArrowClosed },
      })),
    [edges],
  )

  const [rfNodes, setNodes, onNodesChange] = useNodesState(initialNodes)
  const [rfEdges, setEdges, onEdgesChange] = useEdgesState(initialEdges)
  const { updateNodeData } = useReactFlow<Node<GraphNodeData>>()

  useEffect(() => {
    setNodes(initialNodes)
    setEdges(initialEdges)
  }, [initialNodes, initialEdges, setNodes, setEdges])

  // Per-node status ticks: v12's `updateNodeData` patches one node's data in
  // place instead of remapping the whole array — the perf guidance this
  // component is built around (DESIGN.md §8).
  useEffect(() => {
    if (!nodeStatus) return
    for (const [nodeId, update] of Object.entries(nodeStatus)) {
      updateNodeData(nodeId, {
        status: update.status,
        iteration: update.iteration,
        maxIterations: update.max_iterations,
      })
    }
  }, [nodeStatus, updateNodeData])

  const handleNodeClick = useCallback(
    (_event: unknown, node: Node) => onNodeClick?.(node.id),
    [onNodeClick],
  )

  const handleConnect = useCallback(
    (connection: Connection) => {
      if (!connection.source || !connection.target) return
      onConnectNodes?.(connection.source, connection.target)
    },
    [onConnectNodes],
  )

  const handleEdgesDelete = useCallback(
    (deleted: Edge[]) => {
      for (const edge of deleted) onDisconnectNodes?.(edge.source, edge.target)
    },
    [onDisconnectNodes],
  )

  return (
    <ReactFlow
      nodes={rfNodes}
      edges={rfEdges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onConnect={handleConnect}
      onEdgesDelete={handleEdgesDelete}
      nodesConnectable={onConnectNodes !== undefined}
      edgesReconnectable={false}
      deleteKeyCode={onDisconnectNodes ? ['Backspace', 'Delete'] : null}
      nodeTypes={nodeTypes}
      onNodeClick={handleNodeClick}
      fitView
      proOptions={{ hideAttribution: true }}
    >
      <Background />
      <Controls showInteractive={false} />
    </ReactFlow>
  )
}

/** Renders a workflow definition (or a live run over it) as a graph.
 *
 * Route-split: only imported from workflow detail pages (lazy-loaded), so
 * `@xyflow/react` + `dagre` never load on any other dashboard page.
 */
export function WorkflowGraph(props: WorkflowGraphProps) {
  return (
    <ReactFlowProvider>
      <WorkflowGraphInner {...props} />
    </ReactFlowProvider>
  )
}
