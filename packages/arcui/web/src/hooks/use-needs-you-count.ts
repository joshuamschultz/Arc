import { useConnections, useHomeNeeds, usePendingProfileReviewCounts, useRoster } from '@/lib/queries'

/** Everything waiting on the operator, for the nav badge: the home aggregation
 * (approvals, pulse, schedules, capabilities, tasks, questions), connections
 * whose own health record says they need you, and agents with profile facts
 * waiting for review. */
export function useNeedsYouCount(): number {
  const needs = useHomeNeeds().data
  const connections = useConnections().data?.connections ?? []
  const agents = useRoster().data?.agents ?? []
  const profileReviews = usePendingProfileReviewCounts(agents.map((a) => String(a.agent_id)))
  return (
    (needs?.total ?? 0) +
    connections.filter((c) => c.display_status === 'needs_you').length +
    profileReviews.filter((r) => r.count > 0).length
  )
}
