import { ShieldAlert } from 'lucide-react'
import { useState } from 'react'
import { PageHeader } from '@/components/page-header'
import { OperatorModeToggle } from '@/components/operator-mode-toggle'
import { QueryState, EmptyState } from '@/components/states'
import { ApprovalRequest, ContextNote } from '@/components/hitl'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useApprovals } from '@/lib/queries'

export function ApprovalsPage() {
  const [notifications, setNotifications] = useState(() => typeof Notification !== 'undefined' && Notification.permission === 'granted')
  const enableNotifications = async () => {
    if (typeof Notification === 'undefined') return
    const permission = await Notification.requestPermission()
    setNotifications(permission === 'granted')
  }
  const approvals = useApprovals()
  const [operatorMode] = useOperatorMode()

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Approvals"
        description="Actions your agents can't take without your sign-off."
        actions={<div className="flex gap-2"><button type="button" onClick={enableNotifications} aria-pressed={notifications} className="rounded border px-2 py-1 text-xs">{notifications ? 'Notifications on' : 'Enable notifications'}</button><OperatorModeToggle /></div>}
      />
      <div className="flex-1 overflow-auto p-6">
        <QueryState
          query={approvals}
          isEmpty={(data) => data.approvals.length === 0}
          empty={
            <EmptyState
              icon={<ShieldAlert className="size-7" />}
              title="No pending approvals"
              description="When an agent needs your sign-off to act, the request appears here."
            />
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
