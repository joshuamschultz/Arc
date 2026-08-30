import { Outlet } from 'react-router-dom'
import { Sidebar } from './sidebar'
import { OperatorAvatarMenu } from './operator-avatar-menu'
import { ApprovalNotificationListener } from '@/components/approval-notification-listener'
import { CommandPalette } from '@/components/command-palette'

/**
 * App frame: a slim icon rail plus the routed screen. Each screen renders its
 * own header (see PageHeader), so there is no global top bar — the rail
 * carries the brand and theme toggle; operator identity and the view/operator
 * mode toggle live in the fixed top-right avatar menu instead (H-035).
 *
 * The frame is fully keyboard-drivable (H-036): a skip link jumps straight to
 * the routed screen, and the command palette (Cmd/Ctrl-K) reaches every
 * destination and primary action. Both mount here so they exist on every page.
 */
export function AppShell() {
  return (
    <div className="flex h-screen overflow-hidden">
      {/* First in the tab order: let a keyboard user leap past the rail to the
          screen content. Hidden until focused, then a real emerald chip. */}
      <a
        href="#main-content"
        className="sr-only left-4 top-4 z-[60] rounded-[10px] bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow-lg outline-none focus-visible:not-sr-only focus-visible:fixed focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
      >
        Skip to content
      </a>
      <Sidebar />
      <main id="main-content" tabIndex={-1} className="flex flex-1 flex-col overflow-auto outline-none">
        <Outlet />
      </main>
      <OperatorAvatarMenu />
      <ApprovalNotificationListener />
      <CommandPalette />
    </div>
  )
}
