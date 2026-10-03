import { useConnections, useHomeNeeds } from '@/lib/queries'

/** Everything waiting on the operator, for the nav badge: the home aggregation
 * (approvals, pulse, schedules, capabilities, tasks, questions) plus connections
 * whose own health record says they need you. */
export function useNeedsYouCount(): number {
  const needs = useHomeNeeds().data
  const connections = useConnections().data?.connections ?? []
  return (needs?.total ?? 0) + connections.filter((c) => c.display_status === 'needs_you').length
}
