import { NavLink } from 'react-router-dom'
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import { NAV_ITEMS } from '@/app/nav'
import { cn } from '@/lib/utils'
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip'

interface SidebarProps {
  collapsed: boolean
  onToggle: () => void
}

export function Sidebar({ collapsed, onToggle }: SidebarProps) {
  return (
    <nav
      className={cn(
        'flex h-full flex-col border-r border-sidebar-border bg-sidebar py-3 transition-[width] duration-200 ease-out',
        collapsed ? 'w-[56px]' : 'w-[220px]',
      )}
      aria-label="Primary"
    >
      <ul className="flex flex-1 flex-col gap-0.5 px-2">
        {NAV_ITEMS.filter((i) => !i.hidden).map((item) => {
          const link = (
            <NavLink
              to={`/${item.path}`}
              className={({ isActive }) =>
                cn(
                  'group relative flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-sidebar-foreground/65 transition-colors duration-150',
                  'hover:bg-sidebar-accent/60 hover:text-sidebar-foreground',
                  'before:absolute before:left-0 before:top-1/2 before:h-5 before:w-[2px] before:-translate-y-1/2 before:rounded-full before:bg-sidebar-primary before:opacity-0 before:transition-opacity before:duration-150',
                  isActive &&
                    'bg-sidebar-primary/10 text-sidebar-foreground before:opacity-100',
                  collapsed && 'justify-center px-0',
                )
              }
            >
              <item.icon className="size-[18px] shrink-0" />
              {!collapsed && <span className="truncate">{item.label}</span>}
            </NavLink>
          )
          return (
            <li key={item.path}>
              {collapsed ? (
                <Tooltip>
                  <TooltipTrigger asChild>{link}</TooltipTrigger>
                  <TooltipContent side="right">{item.label}</TooltipContent>
                </Tooltip>
              ) : (
                link
              )}
            </li>
          )
        })}
      </ul>

      <div className="px-2">
        <button
          type="button"
          onClick={onToggle}
          className={cn(
            'flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm font-medium text-sidebar-foreground/55 transition-colors duration-150 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground',
            collapsed && 'justify-center px-0',
          )}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          {collapsed ? (
            <PanelLeftOpen className="size-[18px]" />
          ) : (
            <>
              <PanelLeftClose className="size-[18px]" />
              <span>Collapse</span>
            </>
          )}
        </button>
      </div>
    </nav>
  )
}
