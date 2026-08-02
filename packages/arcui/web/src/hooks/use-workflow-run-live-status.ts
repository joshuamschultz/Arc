import { useEffect, useRef, useState } from 'react'
import { useTeamStream, type TeamFrame } from './use-team-stream'
import type { WorkflowNodeStatus, WorkflowRunNodeStatus } from '@/lib/types'

/**
 * A workflow-run status frame carried on the workflow's bound channel
 * (COMP-010 narration, SPEC-061). Reuses the EXISTING `/ws/team` live stream
 * (`useTeamStream`) rather than adding a second polling loop, per DESIGN.md
 * §8's Research Insights.
 *
 * **Merge-reconciliation note:** COMP-010's narrator is a concurrent,
 * not-yet-merged workstream. This is the wire shape this hook assumes for a
 * per-node status tick — a `TeamFrame` whose `type` is
 * `"workflow_run_status"` carrying one node's new status. If the real
 * narrator emits a different shape, one side must yield to the other in
 * review; the important part to preserve is that this stays a `TeamFrame`
 * variant on the SAME `/ws/team` socket, not a new connection.
 */
export interface WorkflowRunStatusFrame extends TeamFrame {
  type: 'workflow_run_status'
  run_id: string
  node_id: string
  status: WorkflowNodeStatus
  iteration?: number | null
  max_iterations?: number | null
  task_run_id?: string | null
}

function isWorkflowRunStatusFrame(frame: TeamFrame): frame is WorkflowRunStatusFrame {
  return frame.type === 'workflow_run_status'
}

/**
 * Reduces the channel's live frames into a `{node_id: status}` map for one
 * run, throttled into the store once per animation frame (DESIGN.md §8) —
 * a flood of per-node ticks under a wide graph coalesces into a single
 * re-render per frame instead of one per websocket message.
 *
 * Caller contract: render the component that calls this hook with
 * `key={runId}` (see `RunGraph` in `pages/workflow-detail.tsx`). Switching
 * runs then remounts this hook's state instead of needing an in-hook reset
 * — the idiomatic React answer to "state must not leak across an identity
 * change", and it sidesteps writing to refs during render entirely.
 */
export function useWorkflowRunLiveStatus(
  channel: string | null,
  runId: string | null,
): Record<string, WorkflowRunNodeStatus> {
  const { frames } = useTeamStream(channel)
  const [status, setStatus] = useState<Record<string, WorkflowRunNodeStatus>>({})
  const bufferRef = useRef<Record<string, WorkflowRunNodeStatus>>({})
  const scheduledRef = useRef(false)
  const processedCountRef = useRef(0)

  useEffect(() => {
    if (!runId) return
    for (let i = processedCountRef.current; i < frames.length; i++) {
      const frame = frames[i]
      if (isWorkflowRunStatusFrame(frame) && frame.run_id === runId) {
        bufferRef.current[frame.node_id] = {
          node_id: frame.node_id,
          status: frame.status,
          iteration: frame.iteration,
          max_iterations: frame.max_iterations,
          task_run_id: frame.task_run_id,
        }
      }
    }
    processedCountRef.current = frames.length

    if (!scheduledRef.current) {
      scheduledRef.current = true
      requestAnimationFrame(() => {
        scheduledRef.current = false
        setStatus({ ...bufferRef.current })
      })
    }
  }, [frames, runId])

  return status
}
