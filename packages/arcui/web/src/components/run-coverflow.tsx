import { useState } from 'react'
import { motion } from 'motion/react'
import { ArrowRight, ChevronDown } from 'lucide-react'
import { StatusChip } from '@/components/ai'
import { EmptyState } from '@/components/states'
import { initials, relativeTime, shortId } from '@/lib/format'
import type { RunSummary } from '@/lib/types'

// Render this many at a time; a huge fleet history stays smooth by revealing
// runs in pages instead of mounting hundreds of motion nodes at once.
const PAGE = 60

/**
 * Flip — a scannable, motion-y wall of runs. Instead of one card floating in a
 * sea of empty canvas, every run is a tile in a responsive grid that springs in
 * on a stagger, lifts on hover, and scrolls. Click any tile to open its trace.
 */
export function RunCoverflow({
  runs,
  resolveName,
  colorFor,
  onOpen,
}: {
  runs: RunSummary[]
  resolveName: (r: RunSummary) => string
  colorFor: (name: string) => string | undefined
  onOpen: (r: RunSummary) => void
}) {
  const [shown, setShown] = useState(PAGE)

  if (!runs.length) {
    return (
      <div className="grid h-full place-items-center p-8">
        <EmptyState title="No runs to flip through" />
      </div>
    )
  }

  const visible = runs.slice(0, shown)
  const remaining = runs.length - visible.length

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-5 py-3">
        <span className="text-sm font-medium text-foreground">Run wall</span>
        <span className="font-mono text-xs tabular-nums text-muted-foreground">
          {visible.length} / {runs.length}
        </span>
      </div>

      <div className="flex-1 overflow-y-auto p-5">
        <div className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-4">
          {visible.map((r, i) => {
            const name = resolveName(r)
            // Stagger only within the freshest page so revealing more never
            // waits on a long tail of delays.
            const delay = Math.min(i % PAGE, 24) * 0.012
            return (
              <motion.button
                key={r.run_id}
                type="button"
                onClick={() => onOpen(r)}
                initial={{ opacity: 0, y: 18, scale: 0.96 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                transition={{ type: 'spring', stiffness: 320, damping: 26, delay }}
                whileHover={{ y: -4, scale: 1.02 }}
                className="group flex flex-col rounded-2xl border border-border bg-card p-4 text-left shadow-sm transition-shadow hover:shadow-lg hover:border-primary/40"
              >
                <div className="flex items-center gap-2.5">
                  <span
                    className="grid size-9 shrink-0 place-items-center rounded-lg text-xs font-semibold text-white"
                    style={{ background: colorFor(name) || 'var(--primary)' }}
                  >
                    {initials(name)}
                  </span>
                  <div className="min-w-0">
                    <div className="truncate font-semibold text-foreground">{name}</div>
                    <div className="font-mono text-[11px] text-muted-foreground">
                      {shortId(r.run_id, 12)}
                    </div>
                  </div>
                  <span className="ml-auto">
                    <StatusChip value={r.status} />
                  </span>
                </div>
                <div className="mt-4 font-display text-base font-bold tracking-tight text-foreground">
                  {r.turns} turns · {r.tool_calls} tools
                </div>
                <div className="mt-3 flex items-center justify-between border-t border-border pt-3 text-[11px] text-muted-foreground">
                  <span>{relativeTime(r.started_at)}</span>
                  <span className="flex items-center gap-1 font-semibold text-primary opacity-0 transition-opacity group-hover:opacity-100">
                    Open trace <ArrowRight className="size-3" />
                  </span>
                </div>
              </motion.button>
            )
          })}
        </div>

        {remaining > 0 && (
          <div className="mt-6 flex justify-center">
            <button
              type="button"
              onClick={() => setShown((n) => n + PAGE)}
              className="flex items-center gap-2 rounded-lg border border-border px-4 py-2 text-sm font-medium text-muted-foreground hover:text-foreground hover:border-primary/40"
            >
              <ChevronDown className="size-4" />
              Show {Math.min(remaining, PAGE)} more
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
