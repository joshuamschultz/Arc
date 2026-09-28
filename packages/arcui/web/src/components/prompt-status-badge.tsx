import { ShieldAlert } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import type { PromptStatus } from '@/lib/types'

/** A prompt's status as the agent itself decides it. `rejected` means an override
 *  is on disk but fails the agent's signature check, so the agent refuses to run;
 *  the reason names which check failed. */
export function PromptStatusBadge({
  status,
  reason,
}: {
  status: PromptStatus
  reason?: string | null
}) {
  if (status === 'rejected') {
    return (
      <span className="flex flex-col gap-1">
        <Badge variant="destructive">
          <ShieldAlert />
          Rejected — agent will refuse to run
        </Badge>
        {reason && <span className="text-xs text-destructive">{reason}</span>}
      </span>
    )
  }
  return <Badge variant={status === 'overridden' ? 'default' : 'secondary'}>{status}</Badge>
}
