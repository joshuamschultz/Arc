import type { WorkflowFailureReason } from '@/lib/types'

/** One line for the run list: the node that failed and why, in plain words. */
export function failureLine(reason?: WorkflowFailureReason | null): string | null {
  if (!reason) return null
  return reason.node_id ? `${reason.node_id}: ${reason.summary}` : reason.summary
}
