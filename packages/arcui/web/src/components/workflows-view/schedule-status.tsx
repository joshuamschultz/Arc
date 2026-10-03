import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api'
import { fmtTime } from '@/lib/format'
import { useApproveWorkflowSchedule, useEnableWorkflowSchedule } from '@/lib/queries'
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
    case 'unapproved':
      return { text: 'Needs approval — re-create or approve', paused: true }
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
  const approve = useApproveWorkflowSchedule(workflowId, schedule)
  const needsApproval = schedule.disabled_reason === 'unapproved'
  const [error, setError] = useState<string | null>(null)
  const { text, paused } = describe(schedule)

  const act = async () => {
    setError(null)
    try {
      await (needsApproval ? approve : enable).mutateAsync()
    } catch (e) {
      const fallback = needsApproval
        ? 'Could not approve the schedule'
        : 'Could not re-enable the schedule'
      setError(e instanceof ApiError ? e.message : fallback)
    }
  }
  const pending = enable.isPending || approve.isPending
  const label = needsApproval ? 'Approve' : 'Re-enable'

  return (
    <span className="flex flex-wrap items-center gap-2">
      <span className={paused ? 'text-status-warning' : undefined}>{text}</span>
      {!schedule.enabled && schedule.last_error && (
        <span className="text-status-error">{schedule.last_error}</span>
      )}
      {paused && (
        <Button size="sm" variant="outline" disabled={pending} onClick={act}>
          {pending ? 'Working…' : label}
        </Button>
      )}
      {error && <span className="text-status-error">{error}</span>}
    </span>
  )
}
