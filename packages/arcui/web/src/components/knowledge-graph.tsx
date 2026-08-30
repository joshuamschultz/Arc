import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from 'd3-force'
import { RotateCcw, Waypoints, ZoomIn, ZoomOut } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { EmptyState, ErrorState, LoadingRows } from '@/components/states'
import { useKnowledgeGraph, type GraphEdge, type GraphNode } from '@/lib/queries'

// H-016 — interactive knowledge-graph viewer. Self-contained: d3-force drives
// the physics simulation, rendering is plain SVG (no CDN, bundled by vite),
// and pan/zoom/drag are hand-rolled pointer-event handlers so no extra
// interaction library is needed on top of d3-force.

interface SimNode extends SimulationNodeDatum, GraphNode {}
interface SimLink extends SimulationLinkDatum<SimNode> {
  kind: string
  weight: number
  salience?: number
  last_hit?: string | null
  hits?: number
}

const WIDTH = 800
const HEIGHT = 520
const MIN_SCALE = 0.2
const MAX_SCALE = 4

/** Deterministic categorical color from a string — no chromatic-scale import. */
function colorForKey(key: string): string {
  let hash = 0
  for (let i = 0; i < key.length; i += 1) hash = (hash * 31 + key.charCodeAt(i)) >>> 0
  return `hsl(${hash % 360}, 62%, 56%)`
}

function nodeColor(node: GraphNode): string {
  if (node.node_type !== 'entity') return 'var(--muted-foreground)'
  const entityType =
    typeof node.metadata.entity_type === 'string' ? node.metadata.entity_type : 'entity'
  return colorForKey(entityType)
}

function nodeLabel(node: GraphNode): string {
  const name = node.metadata.name
  return typeof name === 'string' && name.length > 0 ? name : node.id
}

interface View {
  x: number
  y: number
  k: number
}

/**
 * Drives a d3-force simulation off `rawNodes`/`rawEdges` and republishes each
 * tick as React state — the ONLY store the render path reads (no ref access
 * during render). Positions persist across a re-center (looked up by id from
 * `positions`) so the part of the graph that didn't change doesn't reshuffle.
 */
function useGraphSimulation(rawNodes: GraphNode[], rawEdges: GraphEdge[]) {
  const simRef = useRef<Simulation<SimNode, SimLink> | null>(null)
  const positions = useRef<Map<string, { x: number; y: number }>>(new Map())
  const [renderNodes, setRenderNodes] = useState<SimNode[]>([])
  const [renderLinks, setRenderLinks] = useState<SimLink[]>([])

  useEffect(() => {
    simRef.current?.stop()
    simRef.current = null
    if (rawNodes.length === 0) {
      return
    }

    const simNodes: SimNode[] = rawNodes.map((n) => {
      const prior = positions.current.get(n.id)
      return { ...n, x: prior?.x, y: prior?.y }
    })
    const byId = new Map(simNodes.map((n) => [n.id, n]))
    const simLinks: SimLink[] = rawEdges
      .filter((e) => byId.has(e.src) && byId.has(e.dst))
      .map((e: GraphEdge) => ({
        source: byId.get(e.src)!,
        target: byId.get(e.dst)!,
        kind: e.kind,
        weight: e.weight,
        salience: e.salience,
        last_hit: e.last_hit,
        hits: e.hits,
      }))

    const sim = forceSimulation(simNodes)
      .force(
        'link',
        forceLink<SimNode, SimLink>(simLinks)
          .id((n) => n.id)
          .distance(70)
          .strength(0.4),
      )
      .force('charge', forceManyBody().strength(-140))
      .force('center', forceCenter(0, 0))
      .force('collide', forceCollide(20))
      .on('tick', () => {
        for (const n of simNodes) {
          if (n.x !== undefined && n.y !== undefined) {
            positions.current.set(n.id, { x: n.x, y: n.y })
          }
        }
        setRenderNodes([...simNodes])
        setRenderLinks([...simLinks])
      })

    simRef.current = sim
    return () => {
      sim.stop()
    }
  }, [rawNodes, rawEdges])

  return { simRef, renderNodes, renderLinks }
}

/**
 * Interactive windowed graph view for one agent's associative memory graph.
 * Re-centering (clicking a node) re-queries the operator's gated `graph()`
 * endpoint with `node=<id>` — the same no-read-up gate the initial load used,
 * so an expand can never surface anything the caller wasn't already cleared for.
 */
export function GraphViewer({ agentId }: { agentId: string }) {
  const [center, setCenter] = useState<string | null>(null)
  const query = useKnowledgeGraph(agentId, center, 1)

  const nodes = useMemo(() => query.data?.nodes ?? [], [query.data])
  const edges = useMemo(() => query.data?.edges ?? [], [query.data])
  const { simRef, renderNodes, renderLinks } = useGraphSimulation(nodes, edges)

  const containerRef = useRef<HTMLDivElement>(null)
  const svgRef = useRef<SVGSVGElement>(null)

  const [view, setView] = useState<View>({ x: WIDTH / 2, y: HEIGHT / 2, k: 1 })
  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null)
  const [hoveredEdgeKey, setHoveredEdgeKey] = useState<string | null>(null)
  const [pointer, setPointer] = useState<{ x: number; y: number } | null>(null)
  const dragRef = useRef<
    | { kind: 'pan'; startClientX: number; startClientY: number; startView: View }
    | { kind: 'node'; node: SimNode }
    | null
  >(null)

  const hoveredNode = renderNodes.find((n) => n.id === hoveredNodeId) ?? null
  const hoveredEdge =
    renderLinks.find((l) => `${l.kind}:${(l.source as SimNode).id}:${(l.target as SimNode).id}` === hoveredEdgeKey) ??
    null

  const clientToGraph = useCallback(
    (clientX: number, clientY: number) => {
      const rect = svgRef.current?.getBoundingClientRect()
      const localX = rect ? clientX - rect.left : clientX
      const localY = rect ? clientY - rect.top : clientY
      return { x: (localX - view.x) / view.k, y: (localY - view.y) / view.k }
    },
    [view],
  )

  // Non-passive wheel listener: React's onWheel is passive, and zoom needs
  // preventDefault so the page doesn't scroll under the graph.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const rect = el.getBoundingClientRect()
      const cx = e.clientX - rect.left
      const cy = e.clientY - rect.top
      setView((prev) => {
        const factor = Math.exp(-e.deltaY * 0.001)
        const k = Math.min(MAX_SCALE, Math.max(MIN_SCALE, prev.k * factor))
        // Zoom around the cursor: keep the graph point under the cursor fixed.
        const x = cx - ((cx - prev.x) / prev.k) * k
        const y = cy - ((cy - prev.y) / prev.k) * k
        return { x, y, k }
      })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  const handleBackgroundPointerDown = (e: React.PointerEvent) => {
    dragRef.current = {
      kind: 'pan',
      startClientX: e.clientX,
      startClientY: e.clientY,
      startView: view,
    }
    ;(e.target as Element).setPointerCapture(e.pointerId)
  }

  const handleNodePointerDown = (e: React.PointerEvent, node: SimNode) => {
    e.stopPropagation()
    dragRef.current = { kind: 'node', node }
    simRef.current?.alphaTarget(0.3).restart()
    ;(e.target as Element).setPointerCapture(e.pointerId)
  }

  const handlePointerMove = (e: React.PointerEvent) => {
    setPointer({ x: e.clientX, y: e.clientY })
    const drag = dragRef.current
    if (!drag) return
    if (drag.kind === 'pan') {
      const dx = e.clientX - drag.startClientX
      const dy = e.clientY - drag.startClientY
      setView({ ...drag.startView, x: drag.startView.x + dx, y: drag.startView.y + dy })
    } else {
      const { x, y } = clientToGraph(e.clientX, e.clientY)
      drag.node.fx = x
      drag.node.fy = y
    }
  }

  const handlePointerUp = () => {
    const drag = dragRef.current
    if (drag?.kind === 'node') {
      drag.node.fx = null
      drag.node.fy = null
      simRef.current?.alphaTarget(0)
    }
    dragRef.current = null
  }

  const zoomBy = (factor: number) => {
    setView((prev) => ({ ...prev, k: Math.min(MAX_SCALE, Math.max(MIN_SCALE, prev.k * factor)) }))
  }

  const resetView = () => {
    setCenter(null)
    setView({ x: WIDTH / 2, y: HEIGHT / 2, k: 1 })
  }

  if (query.isLoading) return <LoadingRows />
  if (query.isError) return <ErrorState error={query.error} />
  if (nodes.length === 0) {
    return (
      <EmptyState
        icon={<Waypoints className="size-5" />}
        title={center ? 'No neighbors found' : 'No graph yet'}
        description={
          center
            ? 'This node has no recorded links, or none are visible at your clearance.'
            : 'Entities and their links will appear here once the agent has learned some.'
        }
      />
    )
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs text-muted-foreground">
          {center ? (
            <>
              Neighborhood of <span className="font-medium text-foreground">{center}</span>
            </>
          ) : (
            <>
              Whole-scope window — {nodes.length} nodes, {edges.length} edges
            </>
          )}
        </div>
        <div className="flex items-center gap-1">
          <Button variant="outline" size="icon" onClick={() => zoomBy(1.25)} aria-label="Zoom in">
            <ZoomIn className="size-4" />
          </Button>
          <Button variant="outline" size="icon" onClick={() => zoomBy(0.8)} aria-label="Zoom out">
            <ZoomOut className="size-4" />
          </Button>
          <Button variant="outline" size="sm" onClick={resetView}>
            <RotateCcw className="size-3.5" />
            Reset
          </Button>
        </div>
      </div>

      <div
        ref={containerRef}
        className="relative overflow-hidden rounded-lg border border-border bg-card"
        style={{ height: HEIGHT }}
      >
        <svg
          ref={svgRef}
          width="100%"
          height="100%"
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          onPointerDown={handleBackgroundPointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerLeave={() => {
            setHoveredNodeId(null)
            setHoveredEdgeKey(null)
          }}
          className="cursor-grab touch-none active:cursor-grabbing"
        >
          <g transform={`translate(${view.x}, ${view.y}) scale(${view.k})`}>
            {renderLinks.map((link) => {
              const source = link.source as SimNode
              const target = link.target as SimNode
              if (source.x === undefined || target.x === undefined) return null
              const key = `${link.kind}:${source.id}:${target.id}`
              const isHovered = hoveredEdgeKey === key
              return (
                <line
                  key={key}
                  x1={source.x}
                  y1={source.y}
                  x2={target.x}
                  y2={target.y}
                  stroke={isHovered ? 'var(--primary)' : 'var(--border)'}
                  strokeWidth={isHovered ? 2.5 : Math.max(1, Math.min(4, link.weight * 3))}
                  onPointerEnter={() => setHoveredEdgeKey(key)}
                  onPointerLeave={() => setHoveredEdgeKey((cur) => (cur === key ? null : cur))}
                  style={{ cursor: 'pointer' }}
                />
              )
            })}
            {renderNodes.map((node) => {
              if (node.x === undefined || node.y === undefined) return null
              const isHovered = hoveredNodeId === node.id
              const isCenter = node.id === center
              return (
                <g
                  key={node.id}
                  transform={`translate(${node.x}, ${node.y})`}
                  onPointerDown={(e) => handleNodePointerDown(e, node)}
                  onPointerEnter={() => setHoveredNodeId(node.id)}
                  onPointerLeave={() => setHoveredNodeId((cur) => (cur === node.id ? null : cur))}
                  onClick={() => setCenter(node.id)}
                  style={{ cursor: 'pointer' }}
                >
                  <circle
                    r={isCenter ? 12 : 8}
                    fill={nodeColor(node)}
                    stroke={isHovered || isCenter ? 'var(--foreground)' : 'var(--card)'}
                    strokeWidth={isHovered || isCenter ? 2 : 1.5}
                  />
                  <text
                    x={0}
                    y={20}
                    textAnchor="middle"
                    fontSize={10}
                    fill="var(--muted-foreground)"
                    style={{ pointerEvents: 'none', userSelect: 'none' }}
                  >
                    {nodeLabel(node)}
                  </text>
                </g>
              )
            })}
          </g>
        </svg>

        {hoveredNode && pointer && (
          <div
            className="pointer-events-none absolute z-10 max-w-xs rounded-md border border-border bg-popover px-3 py-2 text-xs text-popover-foreground shadow-md"
            style={{ left: pointer.x + 12, top: pointer.y + 12, position: 'fixed' }}
          >
            <div className="font-semibold">{nodeLabel(hoveredNode)}</div>
            <div className="text-muted-foreground">
              {hoveredNode.node_type}
              {' · '}
              {hoveredNode.classification}
            </div>
            {Object.entries(hoveredNode.metadata)
              .filter(([k]) => k !== 'name')
              .map(([k, v]) => (
                <div key={k} className="text-muted-foreground">
                  {k}: {Array.isArray(v) ? v.join(', ') : String(v)}
                </div>
              ))}
          </div>
        )}

        {hoveredEdge && pointer && !hoveredNode && (
          <div
            className="pointer-events-none absolute z-10 max-w-xs rounded-md border border-border bg-popover px-3 py-2 text-xs text-popover-foreground shadow-md"
            style={{ left: pointer.x + 12, top: pointer.y + 12, position: 'fixed' }}
          >
            <div className="font-semibold">{hoveredEdge.kind}</div>
            <div className="text-muted-foreground">weight: {hoveredEdge.weight.toFixed(2)}</div>
            {hoveredEdge.salience !== undefined && (
              <div className="text-muted-foreground">
                salience: {hoveredEdge.salience.toFixed(2)}
              </div>
            )}
            {hoveredEdge.hits !== undefined && (
              <div className="text-muted-foreground">hits: {hoveredEdge.hits}</div>
            )}
            {hoveredEdge.last_hit && (
              <div className="text-muted-foreground">last hit: {hoveredEdge.last_hit}</div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
