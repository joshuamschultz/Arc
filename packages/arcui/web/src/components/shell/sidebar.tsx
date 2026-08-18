import { Fragment } from 'react'
import { NavLink } from 'react-router-dom'
import { Moon, Sun, Lock, LockOpen } from 'lucide-react'
import { NAV_ITEMS, NAV_GROUPS } from '@/app/nav'
import { cn } from '@/lib/utils'
import { useTheme } from '@/hooks/use-theme'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip'

/**
 * Slim icon rail. Fourteen destinations read as five clusters separated by
 * hairlines; labels live in right-side tooltips. The brand mark sits on top,
 * the theme toggle and operator avatar on the bottom.
 */
export function Sidebar() {
  const { dark, toggle } = useTheme()
  const [operator, setOperator] = useOperatorMode()

  return (
    <nav
      className="flex h-full w-[64px] shrink-0 flex-col items-center border-r border-sidebar-border bg-sidebar py-3"
      aria-label="Primary"
    >
      <div className="mb-3 grid size-9 place-items-center rounded-[10px] bg-primary shadow-sm">
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

      <div className="flex flex-1 flex-col items-center gap-1">
        {NAV_GROUPS.map((group, gi) => {
          const items = NAV_ITEMS.filter((i) => i.group === group && !i.hidden)
          if (!items.length) return null
          return (
            <Fragment key={group}>
              {gi > 0 && <div className="my-1.5 h-px w-6 bg-sidebar-border" />}
              {items.map((item) => (
                <Tooltip key={item.path}>
                  <TooltipTrigger asChild>
                    <NavLink
                      to={`/${item.path}`}
                      aria-label={item.label}
                      className={({ isActive }) =>
                        cn(
                          'grid size-11 place-items-center rounded-[11px] text-sidebar-foreground/60 transition-colors duration-150 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground',
                          isActive &&
                            'bg-primary/12 text-primary hover:bg-primary/12 hover:text-primary',
                        )
                      }
                    >
                      <item.icon className="size-[20px]" />
                    </NavLink>
                  </TooltipTrigger>
                  <TooltipContent side="right">{item.label}</TooltipContent>
                </Tooltip>
              ))}
            </Fragment>
          )
        })}
      </div>

      <div className="mt-2 flex flex-col items-center gap-1.5">
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={() => setOperator(!operator)}
              aria-label={operator ? 'Operator controls on' : 'Operator controls off'}
              className={cn(
                'grid size-11 place-items-center rounded-[11px] transition-colors',
                operator
                  ? 'bg-primary/12 text-primary'
                  : 'text-sidebar-foreground/60 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground',
              )}
            >
              {operator ? <LockOpen className="size-[18px]" /> : <Lock className="size-[18px]" />}
            </button>
          </TooltipTrigger>
          <TooltipContent side="right">
            {operator ? 'Operator controls on' : 'Operator controls off'}
          </TooltipContent>
        </Tooltip>
        <button
          type="button"
          onClick={toggle}
          aria-label={dark ? 'Switch to light mode' : 'Switch to dark mode'}
          className="grid size-11 place-items-center rounded-[11px] text-sidebar-foreground/60 transition-colors hover:bg-sidebar-accent/60 hover:text-sidebar-foreground"
        >
          {dark ? <Sun className="size-[18px]" /> : <Moon className="size-[18px]" />}
        </button>
        <div className="grid size-9 place-items-center rounded-[10px] bg-secondary text-[11px] font-bold text-secondary-foreground">
          JS
        </div>
      </div>
    </nav>
  )
}
