import { useState } from 'react'
import { RotateCcw, ShieldCheck, ShieldX } from 'lucide-react'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { FieldHelp } from '@/components/help'
import { Button } from '@/components/ui/button'
import { EmptyState, QueryState } from '@/components/states'
import { Chip, SourceSelect } from '@/components/knowledge-connection-bits'
import { useProfileReviews, useResolveProfileReview } from '@/lib/queries'

export function ProfileReviewSection({ agentId }: { agentId: string }) {
  const [status, setStatus] = useState('pending')
  const [source, setSource] = useState('')
  const reviews = useProfileReviews(agentId, status, source)
  const resolve = useResolveProfileReview(agentId)

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Select value={status} onValueChange={setStatus}>
          <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="pending">Pending review</SelectItem>
            <SelectItem value="approved">Approved profile</SelectItem>
            <SelectItem value="declined">Declined</SelectItem>
            <SelectItem value="undone">Undone</SelectItem>
          </SelectContent>
        </Select>
        <FieldHelp helpKey="knowledge.mapping.status" route="knowledge" />
        <SourceSelect agentId={agentId} value={source} onChange={setSource} placeholder="All sources" />
      </div>
      <p className="text-xs text-muted-foreground">
        Inferred profile facts never enter agent context until you approve them. Approved facts are the only profile facts agents can recall.
      </p>
      <QueryState
        query={reviews}
        isEmpty={(data) => data.items.length === 0}
        empty={<EmptyState title={status === 'pending' ? 'No profile facts awaiting review' : 'No profile facts in this state'} />}
      >
        {(data) => (
          <ul className="space-y-2">
            {data.items.map((item) => (
              <li key={item.fact_id} className="space-y-2 rounded-lg border border-border bg-muted/20 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-foreground">{item.field}</span>
                  <Chip>{item.kind}</Chip><Chip>{item.classification}</Chip><Chip>{item.status}</Chip>
                  <span className="font-mono text-[11px] text-muted-foreground">{item.source_id}</span>
                </div>
                <p className="text-sm text-foreground">{item.value}</p>
                <div className="flex flex-wrap gap-2">
                  {item.status === 'pending' && (
                    <>
                      <Button size="sm" disabled={resolve.isPending} onClick={() => resolve.mutate({ factId: item.fact_id, decision: 'approve' })}>
                        <ShieldCheck className="size-3.5" /> Approve
                      </Button>
                      <Button size="sm" variant="outline" disabled={resolve.isPending} onClick={() => resolve.mutate({ factId: item.fact_id, decision: 'decline' })}>
                        <ShieldX className="size-3.5" /> Decline
                      </Button>
                    </>
                  )}
                  {item.status === 'approved' && (
                    <Button size="sm" variant="outline" disabled={resolve.isPending} onClick={() => resolve.mutate({ factId: item.fact_id, decision: 'undo' })}>
                      <RotateCcw className="size-3.5" /> Undo approval
                    </Button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </QueryState>
      {resolve.isError && <p role="alert" className="text-xs text-destructive">{resolve.error.message}</p>}
    </div>
  )
}
