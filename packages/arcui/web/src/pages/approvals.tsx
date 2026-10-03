import { ShieldAlert } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { OperatorModeToggle } from '@/components/operator-mode-toggle'
import { QueryState, EmptyState } from '@/components/states'
import { ApprovalRequest, ContextNote } from '@/components/hitl'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { NeedsYouInbox } from '@/components/needs-you-inbox'
import { useNeedsYouCount } from '@/hooks/use-needs-you-count'
import { useApprovals } from '@/lib/queries'

export function ApprovalsPage() {
  const approvals = useApprovals()
  const [operatorMode] = useOperatorMode()
  const waiting = useNeedsYouCount()

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Needs you"
        description="Everything waiting on you: approvals, pulse checks, schedules and connections."
        actions={<OperatorModeToggle />}
      />
      <div className="flex-1 overflow-auto p-4 md:p-6">
        <div className="mx-auto mb-3 max-w-3xl">
          <NeedsYouInbox operatorMode={operatorMode} />
        </div>
        <QueryState
          query={approvals}
          isEmpty={(data) => data.approvals.length === 0}
          empty={
            waiting > 0 ? null : (
              <EmptyState
                icon={<ShieldAlert className="size-7" />}
                title="Nothing needs you"
                description="When an agent needs your sign-off to act, the request appears here."
              />
            )
          }
        >
          {(data) => (
            <div className="mx-auto flex max-w-3xl flex-col gap-3">
              <ContextNote tone="signed">
                Every approval you grant is signed under your operator identity and recorded in the
                audit ledger.
              </ContextNote>
              {data.approvals.map((a) => (
                <ApprovalRequest key={a.id} a={a} operatorMode={operatorMode} />
              ))}
            </div>
          )}
        </QueryState>
      </div>
    </div>
  )
}
