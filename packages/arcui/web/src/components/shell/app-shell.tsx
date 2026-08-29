import { Outlet } from 'react-router-dom'
import { Sidebar } from './sidebar'
import { OperatorAvatarMenu } from './operator-avatar-menu'
import { ApprovalNotificationListener } from '@/components/approval-notification-listener'

/**
 * App frame: a slim icon rail plus the routed screen. Each screen renders its
 * own header (see PageHeader), so there is no global top bar — the rail
 * carries the brand and theme toggle; operator identity and the view/operator
 * mode toggle live in the fixed top-right avatar menu instead (H-035).
 */
export function AppShell() {
  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="flex flex-1 flex-col overflow-auto">
        <Outlet />
      </main>
      <OperatorAvatarMenu />
      <ApprovalNotificationListener />
    </div>
  )
}
