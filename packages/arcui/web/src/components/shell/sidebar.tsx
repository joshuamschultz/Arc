import { Fragment } from 'react'
import { NavLink } from 'react-router-dom'
import { Moon, Sun, Lock, LockOpen, PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import { NAV_ITEMS, NAV_GROUPS, GROUP_LABELS } from '@/app/nav'
import { cn } from '@/lib/utils'
import { useTheme } from '@/hooks/use-theme'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useSidebar } from '@/hooks/use-sidebar'
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip'

/**
 * Primary navigation rail. Collapsed, it is a 64px icon rail whose fourteen
 * destinations read as five hairline-separated clusters, with labels in
 * right-side tooltips. Expanded, it grows to show every icon beside its label
 * under a section header per cluster. The choice persists (useSidebar).
 */
export function Sidebar() {
  const { dark, toggle } = useTheme()
  const [operator, setOperator] = useOperatorMode()
  const { expanded, toggle: toggleExpanded } = useSidebar()

  // A control that adapts to width: icon-only with a tooltip when collapsed, a
  // full labelled row when expanded. Used for the footer's non-link buttons.
  const railButton = (
    onClick: () => void,
    label: string,
    icon: React.ReactNode,
    active = false,
  ) => {
    const btn = (
      <button
        type="button"
        onClick={onClick}
        aria-label={label}
        className={cn(
          'flex h-11 items-center rounded-[11px] transition-colors',
          expanded ? 'w-full gap-3 px-3' : 'w-11 justify-center',
          active
            ? 'bg-primary/12 text-primary'
            : 'text-sidebar-foreground/60 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground',
        )}
      >
        <span className="grid size-[18px] shrink-0 place-items-center">{icon}</span>
        {expanded && <span className="truncate text-[13px] font-medium">{label}</span>}
      </button>
    )
    if (expanded) return btn
    return (
      <Tooltip>
        <TooltipTrigger asChild>{btn}</TooltipTrigger>
        <TooltipContent side="right">{label}</TooltipContent>
      </Tooltip>
    )
  }

  return (
    <nav
      className={cn(
        'flex h-full shrink-0 flex-col border-r border-sidebar-border bg-sidebar py-3 transition-[width] duration-200 ease-out',
        expanded ? 'w-[220px] items-stretch px-3' : 'w-[64px] items-center',
      )}
      aria-label="Primary"
    >
      <div className={cn('mb-3 flex items-center', expanded ? 'gap-2.5 px-1' : 'justify-center')}>
        <div className="grid size-9 shrink-0 place-items-center rounded-[10px] bg-primary shadow-sm">
          <svg viewBox="0 0 24 24" fill="none" className="size-5" aria-hidden="true">
            <path
              d="M4 17a8 8 0 0 1 16 0"
              stroke="var(--primary-foreground)"
              strokeWidth="2.4"
              strokeLinecap="round"
            />
            <circle cx="12" cy="19" r="2.2" fill="var(--primary-foreground)" />
          </svg>
        </div>
        {expanded && (
          <span className="text-[15px] font-bold tracking-tight text-sidebar-foreground">ARC</span>
        )}
      </div>

      <div className="flex flex-1 flex-col gap-0.5 overflow-y-auto">
        {NAV_GROUPS.map((group, gi) => {
          const items = NAV_ITEMS.filter((i) => i.group === group && !i.hidden)
          if (!items.length) return null
          return (
            <Fragment key={group}>
              {expanded ? (
                <div
                  className={cn(
                    'px-3 pb-1 pt-3 text-[10px] font-semibold uppercase tracking-[0.1em] text-sidebar-foreground/40',
                    gi === 0 && 'pt-1',
                  )}
                >
                  {GROUP_LABELS[group]}
                </div>
              ) : (
                gi > 0 && <div className="mx-auto my-1.5 h-px w-6 bg-sidebar-border" />
              )}
              {items.map((item) => {
                const link = (
                  <NavLink
                    to={`/${item.path}`}
                    aria-label={item.label}
                    className={({ isActive }) =>
                      cn(
                        'flex h-11 items-center rounded-[11px] text-sidebar-foreground/60 transition-colors duration-150 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground',
                        expanded ? 'gap-3 px-3' : 'w-11 justify-center',
                        isActive &&
                          'bg-primary/12 text-primary hover:bg-primary/12 hover:text-primary',
                      )
                    }
                  >
                    <item.icon className="size-[20px] shrink-0" />
                    {expanded && (
                      <span className="truncate text-[13px] font-medium">{item.label}</span>
                    )}
                  </NavLink>
                )
                if (expanded) return <Fragment key={item.path}>{link}</Fragment>
                return (
                  <Tooltip key={item.path}>
                    <TooltipTrigger asChild>{link}</TooltipTrigger>
                    <TooltipContent side="right">{item.label}</TooltipContent>
                  </Tooltip>
                )
              })}
            </Fragment>
          )
        })}
      </div>

      <div className="mt-2 flex flex-col gap-1.5">
        <div className={cn('flex', expanded ? 'justify-start' : 'justify-center')}>
          {railButton(
            toggleExpanded,
            expanded ? 'Collapse' : 'Expand',
            expanded ? <PanelLeftClose className="size-[18px]" /> : <PanelLeftOpen className="size-[18px]" />,
          )}
        </div>
        {railButton(
          () => setOperator(!operator),
          operator ? 'Operator controls on' : 'Operator controls off',
          operator ? <LockOpen className="size-[18px]" /> : <Lock className="size-[18px]" />,
          operator,
        )}
        {railButton(
          toggle,
          dark ? 'Switch to light mode' : 'Switch to dark mode',
          dark ? <Sun className="size-[18px]" /> : <Moon className="size-[18px]" />,
        )}
        <div className={cn('flex items-center', expanded ? 'gap-3 px-3 pt-1' : 'justify-center pt-1')}>
          <div className="grid size-9 shrink-0 place-items-center rounded-[10px] bg-secondary text-[11px] font-bold text-secondary-foreground">
            JS
          </div>
          {expanded && (
            <span className="truncate text-[13px] font-medium text-sidebar-foreground/70">
              Operator
            </span>
          )}
        </div>
      </div>
    </nav>
  )
}
