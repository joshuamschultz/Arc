import { memo, useCallback, useEffect, useMemo } from 'react'
import {
  Background,
  Controls,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import dagre from 'dagre'
import { cn } from '@/lib/utils'
import type {
  WorkflowEdge as WfEdge,
  WorkflowNode as WfNode,
  WorkflowNodeStatus,
} from '@/lib/types'

// SDD §8 / DESIGN.md Research Insights: dagre (~25KB gz) over elkjs
// (~433KB gz) — elkjs's cycle-breaking strategies are unneeded because
// declared bounded back-edges are already reversed for layout, and dagre's
// feedback-arc reversal handles them.
const DAGRE_NODE_WIDTH = 176
const DAGRE_NODE_HEIGHT = 64

export interface GraphNodeData extends Record<string, unknown> {
  label: string
  kind: string
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

// Memoized per React Flow's perf guidance — a custom node re-renders on every
// prop change otherwise, and with wide graphs that's the dominant cost.
const WorkflowGraphNode = memo(function WorkflowGraphNode({
  data,
  selected,
}: NodeProps & { data: GraphNodeData }) {
  const tone = STATUS_TONE[data.status ?? 'pending'] ?? STATUS_TONE.pending
  return (
    <div
      className={cn(
        'min-w-[160px] rounded-lg border-2 px-3 py-2 text-xs shadow-xs transition-colors',
        tone,
        selected && 'ring-2 ring-ring ring-offset-1 ring-offset-background',
        data.hasError && 'border-destructive',
      )}
    >
      <div className="font-mono text-[10px] uppercase tracking-wide opacity-70">{data.kind}</div>
      <div className="truncate font-semibold text-foreground">{data.label}</div>
      {data.status === 'looping' ? (
        <div className="mt-1 text-[10px]">
          looping {data.iteration ?? 0} of {data.maxIterations ?? '?'}
        </div>
      ) : data.status ? (
        <div className="mt-1 text-[10px] capitalize">{data.status.replace(/_/g, ' ')}</div>
      ) : null}
    </div>
  )
})

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
}: WorkflowGraphProps) {
  const cacheKey = `${workflowId}:${version}`
  const positions = useMemo(() => layoutPositions(cacheKey, nodes, edges), [cacheKey, nodes, edges])

  const initialNodes = useMemo<Node<GraphNodeData>[]>(
    () =>
      nodes.map((n) => {
        const pos = positions.find((p) => p.id === n.id)
        return {
          id: n.id,
          type: 'workflowNode',
          position: { x: pos?.x ?? 0, y: pos?.y ?? 0 },
          data: {
            label: n.id,
            kind: n.kind,
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
    // array. Only a structural change (new nodes/edges/cacheKey) rebuilds it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [nodes, positions, cacheKey, errorNodeIds],
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

  return (
    <ReactFlow
      nodes={rfNodes}
      edges={rfEdges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
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
