import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { create } from 'zustand'
import { Moon, Sun, PanelLeftClose, PanelLeftOpen, Boxes, MessageSquare, ListChecks, type LucideIcon } from 'lucide-react'
import { NAV_ITEMS } from '@/app/nav'
import { agentLabel } from '@/lib/agent-names'
import { useRoster } from '@/lib/queries'
import { useTheme } from './use-theme'
import { useSidebar } from './use-sidebar'

/**
 * Open-state for the global command palette lives in a tiny store so the
 * Cmd/Ctrl-K listener, the palette itself, and any future affordance (a
 * header "⌘K" button) all read and flip one source of truth.
 */
interface CommandPaletteState {
  open: boolean
  setOpen: (open: boolean) => void
  toggle: () => void
}

export const useCommandPaletteStore = create<CommandPaletteState>()((set) => ({
  open: false,
  setOpen: (open) => set({ open }),
  toggle: () => set((s) => ({ open: !s.open })),
}))

/** One selectable row in the palette. */
export interface CommandItem {
  id: string
  label: string
  icon: LucideIcon
  /** Extra terms matched by search beyond the visible label. */
  keywords?: string[]
  run: () => void
}

/** A titled cluster of rows, rendered in order. */
export interface CommandGroup {
  heading: string
  items: CommandItem[]
}

/**
 * Builds the palette's command list: every primary destination, one "open"
 * row per fleet agent, and the primary actions already reachable in the UI
 * (start a run, new task, switch theme, collapse the rail). Each `run` closes
 * the palette first, then acts — the caller never has to remember to close it.
 */
export function useCommandPalette(): CommandGroup[] {
  const navigate = useNavigate()
  const setOpen = useCommandPaletteStore((s) => s.setOpen)
  const { dark, toggle: toggleTheme } = useTheme()
  const { expanded, toggle: toggleSidebar } = useSidebar()
  const roster = useRoster()

  const agents = useMemo(
    () => (roster.data?.agents ?? []).filter((a) => !a.hidden),
    [roster.data],
  )

  return useMemo<CommandGroup[]>(() => {
    // Wrap every action so a selection always dismisses the overlay first.
    const act = (fn: () => void) => () => {
      setOpen(false)
      fn()
    }

    const destinations: CommandItem[] = NAV_ITEMS.filter((i) => !i.hidden).map((item) => ({
      id: `nav:${item.path}`,
      label: item.label,
      icon: item.icon,
      keywords: [item.path, item.group],
      run: act(() => navigate(`/${item.path}`)),
    }))

    const agentItems: CommandItem[] = agents.map((agent) => {
      const label = agentLabel(agent)
      const id = String(agent.agent_id ?? label)
      return {
        id: `agent:${id}`,
        label: `Open ${label}`,
        icon: Boxes,
        keywords: [label, id, 'agent', 'fleet'],
        run: act(() => navigate(`/agents/${id}`)),
      }
    })

    const actions: CommandItem[] = [
      {
        id: 'action:start-run',
        label: 'Start a run',
        icon: MessageSquare,
        keywords: ['chat', 'message', 'run', 'talk'],
        run: act(() => navigate('/messages')),
      },
      {
        id: 'action:new-task',
        label: 'New task',
        icon: ListChecks,
        keywords: ['task', 'create', 'assign'],
        run: act(() => navigate('/tasks')),
      },
      {
        id: 'action:theme',
        label: dark ? 'Switch to light mode' : 'Switch to dark mode',
        icon: dark ? Sun : Moon,
        keywords: ['theme', 'dark', 'light', 'appearance'],
        run: act(toggleTheme),
      },
      {
        id: 'action:sidebar',
        label: expanded ? 'Collapse the sidebar' : 'Expand the sidebar',
        icon: expanded ? PanelLeftClose : PanelLeftOpen,
        keywords: ['sidebar', 'rail', 'navigation', 'collapse', 'expand'],
        run: act(toggleSidebar),
      },
    ]

    const groups: CommandGroup[] = [{ heading: 'Go to', items: destinations }]
    if (agentItems.length) groups.push({ heading: 'Agents', items: agentItems })
    groups.push({ heading: 'Actions', items: actions })
    return groups
  }, [navigate, setOpen, agents, dark, expanded, toggleTheme, toggleSidebar])
}
