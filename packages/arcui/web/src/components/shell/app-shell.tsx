import { Outlet } from 'react-router-dom'
import { Sidebar } from './sidebar'
import { ApprovalNotificationListener } from '@/components/approval-notification-listener'

/**
 * App frame: a slim icon rail plus the routed screen. Each screen renders its
 * own header (see PageHeader), so there is no global top bar — the rail carries
 * the brand, theme toggle, and operator identity instead.
 */
export function AppShell() {
  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <main className="flex flex-1 flex-col overflow-auto">
        <Outlet />
      </main>
      <ApprovalNotificationListener />
    </div>
  )
}
