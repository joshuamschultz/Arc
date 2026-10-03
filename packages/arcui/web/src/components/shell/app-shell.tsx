import { useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { Menu } from 'lucide-react'
import { Sheet, SheetContent, SheetDescription, SheetTitle } from '@/components/ui/sheet'
import { Sidebar } from './sidebar'
import { ApprovalNotificationListener } from '@/components/approval-notification-listener'
import { CommandPalette } from '@/components/command-palette'

/**
 * App frame: a slim icon rail plus the routed screen. Each screen renders its
 * own header (see PageHeader), so there is no global top bar — the rail
 * carries the brand and theme toggle; operator identity and the view/operator
 * mode toggle live in the avatar menu inside each screen header (H-035).
 *
 * The frame is fully keyboard-drivable (H-036): a skip link jumps straight to
 * the routed screen, and the command palette (Cmd/Ctrl-K) reaches every
 * destination and primary action. Both mount here so they exist on every page.
 */
export function AppShell() {
  const { pathname } = useLocation()
  // The drawer is open only for the route it was opened on, so any navigation
  // (link, palette, back button) dismisses it with no effect-driven state sync.
  const [openOn, setOpenOn] = useState<string | null>(null)
  const drawerOpen = openOn === pathname
  const setDrawerOpen = (open: boolean) => setOpenOn(open ? pathname : null)

  return (
    <div className="flex h-dvh flex-col overflow-hidden md:flex-row">
      {/* First in the tab order: let a keyboard user leap past the rail to the
          screen content. Hidden until focused, then a real emerald chip. */}
      <a
        href="#main-content"
        className="sr-only left-4 top-4 z-[60] rounded-[10px] bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow-lg outline-none focus-visible:not-sr-only focus-visible:fixed focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
      >
        Skip to content
      </a>
      {/* Below md the rail becomes a slide-over drawer opened from this bar. */}
      <header className="flex shrink-0 items-center gap-2 border-b border-sidebar-border bg-sidebar px-2 pb-1 pt-[max(0.25rem,env(safe-area-inset-top))] md:hidden">
        <button
          type="button"
          aria-label="Open navigation menu"
          onClick={() => setDrawerOpen(true)}
          className="grid min-h-11 min-w-11 place-items-center rounded-[11px] text-sidebar-foreground outline-none hover:bg-sidebar-accent/60 focus-visible:ring-2 focus-visible:ring-ring/60"
        >
          <Menu className="size-5" />
        </button>
        <span className="text-[15px] font-bold tracking-tight text-sidebar-foreground">ARC</span>
      </header>
      <Sheet open={drawerOpen} onOpenChange={setDrawerOpen}>
        <SheetContent side="left" showCloseButton={false} className="w-[85vw] max-w-[320px] gap-0 border-sidebar-border bg-sidebar p-0 sm:max-w-[320px]">
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          <SheetDescription className="sr-only">Move between sections of the console.</SheetDescription>
          <Sidebar mobile onNavigate={() => setDrawerOpen(false)} />
        </SheetContent>
      </Sheet>
      <Sidebar />
      <main id="main-content" tabIndex={-1} className="flex min-h-0 min-w-0 flex-1 flex-col overflow-auto outline-none">
        <Outlet />
      </main>
      <ApprovalNotificationListener />
      <CommandPalette />
    </div>
  )
}
