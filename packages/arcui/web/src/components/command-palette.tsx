import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { Dialog as DialogPrimitive } from 'radix-ui'
import { Search, CornerDownLeft } from 'lucide-react'
import { cn } from '@/lib/utils'
import {
  useCommandPalette,
  useCommandPaletteStore,
  type CommandItem,
} from '@/hooks/use-command-palette'

/** A visible item paired with the group heading it renders under. */
interface FlatEntry {
  item: CommandItem
  heading: string
  /** True on the first entry of each group — draws the heading above it. */
  first: boolean
}

/**
 * Global command palette (Cmd/Ctrl-K). Radix Dialog gives us the modal overlay,
 * focus trap, Escape-to-close, and focus-return-to-trigger for free; on top of
 * it we run a search box and a roving-`aria-activedescendant` listbox so the
 * whole surface is drivable from the keyboard: type to filter, Up/Down (with
 * wrap) and Home/End to move, Enter to run, Escape to dismiss.
 *
 * Mount once, at the app shell. It owns no navigation of its own — every row's
 * behavior comes from `useCommandPalette`.
 */
export function CommandPalette() {
  const open = useCommandPaletteStore((s) => s.open)
  const setOpen = useCommandPaletteStore((s) => s.setOpen)
  const toggle = useCommandPaletteStore((s) => s.toggle)
  const groups = useCommandPalette()

  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const listRef = useRef<HTMLDivElement>(null)

  // Cmd/Ctrl-K from anywhere opens (or closes) the palette.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault()
        toggle()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [toggle])

  // Reset the search and selection every time the palette opens.
  useEffect(() => {
    if (open) {
      setQuery('')
      setActive(0)
    }
  }, [open])

  // Flatten the visible, filtered rows into one list the arrow keys walk.
  const entries = useMemo<FlatEntry[]>(() => {
    const needle = query.trim().toLowerCase()
    const match = (item: CommandItem) => {
      if (!needle) return true
      const hay = [item.label, ...(item.keywords ?? [])].join(' ').toLowerCase()
      return hay.includes(needle)
    }
    const out: FlatEntry[] = []
    for (const group of groups) {
      const items = group.items.filter(match)
      items.forEach((item, i) =>
        out.push({ item, heading: group.heading, first: i === 0 }),
      )
    }
    return out
  }, [groups, query])

  // Keep the active index inside the current result set as filtering narrows it.
  useEffect(() => {
    setActive((a) => (a >= entries.length ? Math.max(0, entries.length - 1) : a))
  }, [entries.length])

  // Scroll the active row into view as selection moves.
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  }, [active, entries.length])

  const activeId = entries[active] ? `cmdk-opt-${active}` : undefined

  const onInputKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (!entries.length) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((a) => (a + 1) % entries.length)
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((a) => (a - 1 + entries.length) % entries.length)
    } else if (e.key === 'Home') {
      e.preventDefault()
      setActive(0)
    } else if (e.key === 'End') {
      e.preventDefault()
      setActive(entries.length - 1)
    } else if (e.key === 'Enter') {
      e.preventDefault()
      entries[active]?.item.run()
    }
  }

  return (
    <DialogPrimitive.Root open={open} onOpenChange={setOpen}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay
          className="fixed inset-0 z-50 bg-black/50 backdrop-blur-sm data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0"
        />
        <DialogPrimitive.Content
          aria-label="Command palette"
          className="fixed left-1/2 top-[14vh] z-50 w-[calc(100%-2rem)] max-w-xl -translate-x-1/2 overflow-hidden rounded-[14px] border border-border bg-popover text-popover-foreground shadow-2xl outline-none data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95"
        >
          <DialogPrimitive.Title className="sr-only">Command palette</DialogPrimitive.Title>
          <DialogPrimitive.Description className="sr-only">
            Search for a destination or action, then press Enter to run it.
          </DialogPrimitive.Description>

          <div className="flex items-center gap-2.5 border-b border-border px-4">
            <Search className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            <input
              autoFocus
              type="text"
              role="combobox"
              aria-expanded="true"
              aria-controls="cmdk-list"
              aria-activedescendant={activeId}
              aria-label="Search destinations and actions"
              placeholder="Search destinations and actions…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={onInputKeyDown}
              className="h-12 w-full bg-transparent text-sm text-foreground outline-none placeholder:text-muted-foreground"
            />
          </div>

          <div
            ref={listRef}
            id="cmdk-list"
            role="listbox"
            aria-label="Results"
            className="max-h-[52vh] overflow-y-auto p-1.5"
          >
            {entries.length === 0 ? (
              <div className="px-3 py-6 text-center text-sm text-muted-foreground">
                No matches for “{query}”.
              </div>
            ) : (
              entries.map((entry, i) => {
                const Icon = entry.item.icon
                const isActive = i === active
                return (
                  <div key={entry.item.id}>
                    {entry.first && (
                      <div className="px-2 pb-1 pt-2.5 text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
                        {entry.heading}
                      </div>
                    )}
                    <div
                      id={`cmdk-opt-${i}`}
                      data-index={i}
                      role="option"
                      aria-selected={isActive}
                      onMouseMove={() => setActive(i)}
                      onClick={() => entry.item.run()}
                      className={cn(
                        'flex cursor-pointer items-center gap-3 rounded-[9px] px-2.5 py-2 text-sm',
                        isActive
                          ? 'bg-accent text-accent-foreground'
                          : 'text-foreground/80',
                      )}
                    >
                      <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                      <span className="truncate">{entry.item.label}</span>
                      {isActive && (
                        <CornerDownLeft
                          className="ml-auto size-3.5 shrink-0 text-muted-foreground"
                          aria-hidden="true"
                        />
                      )}
                    </div>
                  </div>
                )
              })
            )}
          </div>

          <div className="flex items-center gap-3 border-t border-border px-4 py-2 text-[11px] text-muted-foreground">
            <span><kbd className="font-sans">↑</kbd> <kbd className="font-sans">↓</kbd> to move</span>
            <span><kbd className="font-sans">↵</kbd> to run</span>
            <span><kbd className="font-sans">esc</kbd> to close</span>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}
