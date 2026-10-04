import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { GateCard } from '@/components/gate-card'
import { RunDetailDrawer } from '@/components/run-detail-drawer'
import { StatusText } from '@/components/status-badge'
import { WorkflowGraph, type NodeStatusUpdate } from '@/components/workflow-graph'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useWorkflowRunLiveStatus } from '@/hooks/use-workflow-run-live-status'
import { fmtTime, shortId } from '@/lib/format'
import { useRoster, useWorkflowRun } from '@/lib/queries'
import type { RunSummary, WorkflowDetail } from '@/lib/types'
import { NodeDetail, RunError } from './node-detail'

/** Wall-clock duration between two ISO stamps, as a short human string. */
function fmtDuration(start?: string | null, end?: string | null): string | null {
  if (!start) return null
  const from = Date.parse(start)
  const to = end ? Date.parse(end) : Date.now()
  if (!Number.isFinite(from) || !Number.isFinite(to) || to < from) return null
  const s = Math.round((to - from) / 1000)
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
}

const NODE_STATUS_TONE: Record<string, string> = {
  done: 'border-status-online/30 bg-status-online/10 text-status-online',
  running: 'border-status-info/30 bg-status-info/10 text-status-info',
  failed: 'border-status-error/30 bg-status-error/10 text-status-error',
  waiting_gate: 'border-status-warning/30 bg-status-warning/10 text-status-warning',
  done_with_failures: 'border-status-warning/30 bg-status-warning/10 text-status-warning',
  skipped: 'border-border bg-muted/30 text-muted-foreground',
  cancelled: 'border-border bg-muted/30 text-muted-foreground line-through',
  routed: 'border-status-info/30 bg-status-info/10 text-status-info',
  in_progress: 'border-status-info/30 bg-status-info/10 text-status-info',
  review: 'border-status-warning/30 bg-status-warning/10 text-status-warning',
  materialized: 'border-border bg-muted/20 text-muted-foreground',
  pending: 'border-border bg-muted/20 text-muted-foreground',
}

/** Live per-node status for one selected run, computed from the REST
 * snapshot (backfill) plus the workflow channel's live frames — reused via
 * `useWorkflowRunLiveStatus` instead of a second polling loop (DESIGN.md §8).
 * A node absent from the run's reached set renders `skipped` once the run is
 * terminal, `pending` while still in flight — lazy materialization means an
 * untaken branch never gets a task row to read a status from.
 */
export function RunTimeline({ workflow, runId }: { workflow: WorkflowDetail; runId: string }) {
  const run = useWorkflowRun(runId, 4000)
  const rosterQ = useRoster()
  const [operatorMode] = useOperatorMode()
  const liveStatus = useWorkflowRunLiveStatus(workflow.channel ?? null, runId)
  const [timelineRun, setTimelineRun] = useState<RunSummary | null>(null)

  // did -> display name, so the feed names the agent that ran each node.
  const ownerName = (did?: string | null): string | null => {
    if (!did) return null
    const a = (rosterQ.data?.agents ?? []).find((x) => x.did === did)
    return a ? String(a.display_name || a.name || a.agent_id || did) : shortId(did, 16)
  }

  const reached = useMemo(() => {
    const map = new Map(run.data?.nodes.map((n) => [n.node_id, n]))
    for (const [nodeId, s] of Object.entries(liveStatus)) map.set(nodeId, s)
    return map
  }, [run.data, liveStatus])

  const nodeStatus = useMemo<Record<string, NodeStatusUpdate>>(() => {
    const terminal = run.data ? ['done', 'failed', 'cancelled'].includes(run.data.status) : false
    const out: Record<string, NodeStatusUpdate> = {}
    for (const n of workflow.nodes) {
      const r = reached.get(n.id)
      out[n.id] = r
        ? { status: r.status }
        : { status: terminal ? 'skipped' : 'pending' }
    }
    return out
  }, [workflow.nodes, reached, run.data])

  const openTimeline = (nodeId: string) => {
    const taskRunId = reached.get(nodeId)?.task_run_id
    if (!taskRunId) return
    setTimelineRun({
      run_id: taskRunId,
      agent: nodeId,
      turns: 0,
      tool_calls: 0,
      llm_calls: 0,
      prompt_tokens: 0,
      completion_tokens: 0,
      total_tokens: 0,
      cost_usd: 0,
      status: nodeStatus[nodeId]?.status ?? 'pending',
    })
  }

  const waitingGates = (run.data?.nodes ?? []).filter(
    (n) => n.status === 'waiting_gate' && n.task_id,
  )

  // The feed reads in execution order: the path actually taken first, then any
  // node with a row that isn't on the path, then the still-pending definition
  // nodes — so a run reads top-to-bottom the way it ran.
  const byId = new Map((run.data?.nodes ?? []).map((n) => [n.node_id, n]))
  const order: string[] = []
  const pushOnce = (id: string) => {
    if (id && !order.includes(id)) order.push(id)
  }
  for (const id of run.data?.path_taken ?? []) pushOnce(id)
  for (const n of run.data?.nodes ?? []) pushOnce(n.node_id)
  for (const n of workflow.nodes) pushOnce(n.id)

  return (
    <>
      {waitingGates.map((gate) => (
        <GateCard
          key={gate.task_id}
          taskId={gate.task_id!}
          nodeId={gate.node_id}
          body={`This run is waiting on ${gate.node_id}.`}
        />
      ))}
      <div className="min-h-[380px] overflow-hidden rounded-lg border border-border">
        <WorkflowGraph
          workflowId={workflow.id}
          version={workflow.version}
          nodes={workflow.nodes}
          edges={workflow.edges}
          nodeStatus={nodeStatus}
          onNodeClick={openTimeline}
        />
      </div>

      {run.data && (
        <RunError
          status={run.data.status}
          lastError={run.data.last_error}
          failureReason={run.data.failure_reason}
        />
      )}

      <div className="space-y-1.5">
        <div className="flex items-center justify-between px-0.5">
          <span className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            Node activity
          </span>
          {run.data && (
            <span className="text-[11px] text-muted-foreground">
              <StatusText value={run.data.status} />
              {run.data.started_at ? ` · started ${fmtTime(run.data.started_at)}` : ''}
              {fmtDuration(run.data.started_at, run.data.ended_at)
                ? ` · ${fmtDuration(run.data.started_at, run.data.ended_at)}`
                : ''}
            </span>
          )}
        </div>
        <div className="divide-y divide-border/60 rounded-lg border border-border">
          {order.map((id) => {
            const rec = byId.get(id)
            const status = nodeStatus[id]?.status ?? rec?.status ?? 'pending'
            const kind = rec?.kind ?? workflow.nodes.find((n) => n.id === id)?.kind
            const owner = ownerName(rec?.owner_did)
            const dur = fmtDuration(rec?.started_at, rec?.completed_at)
            const canOpen = Boolean(rec?.task_run_id)
            return (
              <div key={id}>
              <div
                className={`flex items-center gap-3 px-2.5 py-2 text-sm ${
                  canOpen ? 'cursor-pointer hover:bg-muted/40' : ''
                }`}
                onClick={canOpen ? () => openTimeline(id) : undefined}
              >
                <span
                  className={`inline-flex shrink-0 items-center rounded-md border px-1.5 py-0.5 text-[10px] font-medium capitalize ${
                    NODE_STATUS_TONE[status] ?? NODE_STATUS_TONE.pending
                  }`}
                >
                  {status.replace('_', ' ')}
                </span>
                <span className="min-w-0 flex-1 truncate">
                  <span className="text-foreground">{id}</span>
                  {kind && <span className="ml-1.5 text-[11px] text-muted-foreground">{kind}</span>}
                </span>
                {owner && (
                  <span className="hidden shrink-0 text-[11px] text-muted-foreground sm:inline">
                    {owner}
                  </span>
                )}
                <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground">
                  {rec?.started_at ? fmtTime(rec.started_at) : '—'}
                  {dur ? ` · ${dur}` : ''}
                </span>
                {canOpen && (
                  <Link
                    to={`/arcrun?run=${encodeURIComponent(rec!.task_run_id!)}`}
                    className="shrink-0 text-[11px] text-primary hover:underline"
                    onClick={(e) => e.stopPropagation()}
                  >
                    agent run →
                  </Link>
                )}
              </div>
              {rec && (
                <NodeDetail
                  node={rec}
                  runId={runId}
                  runStatus={run.data?.status}
                  canRetry={operatorMode}
                />
              )}
              </div>
            )
          })}
        </div>
      </div>

      <RunDetailDrawer
        run={timelineRun}
        open={timelineRun !== null}
        onOpenChange={(o) => !o && setTimelineRun(null)}
      />
    </>
  )
}
