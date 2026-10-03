import { useMemo } from 'react'
import { useRoster } from '@/lib/queries'

/** The fleet's agent handles (`@agent_id`), hidden agents excluded. */
export function useAgentHandles(): string[] {
  const roster = useRoster()
  return useMemo(
    () =>
      (roster.data?.agents ?? [])
        .filter((a) => !a.hidden)
        .map((a) => `@${a.agent_id ?? a.name ?? ''}`)
        .filter((h) => h !== '@'),
    [roster.data],
  )
}
