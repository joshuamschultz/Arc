import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api'
import { fmtTime } from '@/lib/format'
import { useEnableWorkflowSchedule } from '@/lib/queries'
import type { WorkflowSchedule } from '@/lib/types'

function describe(schedule: WorkflowSchedule): { text: string; paused: boolean } {
  if (schedule.enabled) {
    return { text: `Scheduled · next ${fmtTime(schedule.next_fire_at)}`, paused: false }
  }
  switch (schedule.disabled_reason) {
    case 'breaker':
      return {
        text: `Paused by safety breaker since ${fmtTime(schedule.disabled_at)}`,
        paused: true,
      }
    case 'archived':
      return { text: 'Schedule off · workflow archived', paused: true }
    default:
      return { text: 'Disabled by operator', paused: true }
  }
}

/**
 * The owner agent's schedule row, in plain words, with the one action an
 * operator needs when it is off. Re-enable goes through the same signed,
 * audited schedule update the schedule editor uses; this page adds no second
 * path to the scheduler.
 */
export function ScheduleStatus({
  workflowId,
  schedule,
}: {
  workflowId: string
  schedule: WorkflowSchedule
}) {
  const enable = useEnableWorkflowSchedule(workflowId, schedule)
  const [error, setError] = useState<string | null>(null)
  const { text, paused } = describe(schedule)

  const reenable = async () => {
    setError(null)
    try {
      await enable.mutateAsync()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not re-enable the schedule')
    }
  }

  return (
    <span className="flex flex-wrap items-center gap-2">
      <span className={paused ? 'text-status-warning' : undefined}>{text}</span>
      {!schedule.enabled && schedule.last_error && (
        <span className="text-status-error">{schedule.last_error}</span>
      )}
      {paused && (
        <Button size="sm" variant="outline" disabled={enable.isPending} onClick={reenable}>
          {enable.isPending ? 'Re-enabling…' : 'Re-enable'}
        </Button>
      )}
      {error && <span className="text-status-error">{error}</span>}
    </span>
  )
}
