import { useRef, useState } from 'react'
import { motion } from 'motion/react'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { StatusChip } from '@/components/ai'
import { EmptyState } from '@/components/states'
import { initials, relativeTime, shortId } from '@/lib/format'
import type { RunSummary } from '@/lib/types'

/**
 * Cover Flow — flip through runs like album art. The centered run is in focus;
 * click it to open its trace, click a side card to bring it to center. Drag,
 * scroll, or arrow-key. Motion springs handle the 3D transitions.
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
  const [center, setCenter] = useState(0)
  const down = useRef(false)
  const startX = useRef(0)
  const acc = useRef(0)

  const go = (n: number) => setCenter(Math.max(0, Math.min(runs.length - 1, n)))

  if (!runs.length) {
    return (
      <div className="grid h-full place-items-center p-8">
        <EmptyState title="No runs to flip through" />
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col">
      <div
        className="relative flex-1 cursor-grab overflow-hidden [perspective:1600px] active:cursor-grabbing"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'ArrowLeft') go(center - 1)
          if (e.key === 'ArrowRight') go(center + 1)
        }}
        onPointerDown={(e) => {
          down.current = true
          startX.current = e.clientX
          ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
        }}
        onPointerMove={(e) => {
          if (!down.current) return
          const dx = e.clientX - startX.current
          if (Math.abs(dx) > 70) {
            go(center + (dx < 0 ? 1 : -1))
            startX.current = e.clientX
          }
        }}
        onPointerUp={() => {
          down.current = false
        }}
        onWheel={(e) => {
          acc.current += e.deltaY + e.deltaX
          if (Math.abs(acc.current) > 60) {
            go(center + (acc.current > 0 ? 1 : -1))
            acc.current = 0
          }
        }}
      >
        <div className="absolute inset-0 flex items-center justify-center">
          {runs.map((r, i) => {
            const o = i - center
            const ax = Math.abs(o)
            const name = resolveName(r)
            return (
              <motion.button
                key={r.run_id}
                type="button"
                onClick={() => (o === 0 ? onOpen(r) : go(i))}
                initial={false}
                animate={{
                  x: o * 250,
                  z: -ax * 180 + (o === 0 ? 60 : 0),
                  rotateY: -o * 40,
                  scale: o === 0 ? 1.06 : 0.9,
                  opacity: ax > 3 ? 0 : 1,
                }}
                transition={{ type: 'spring', stiffness: 260, damping: 30 }}
                style={{
                  position: 'absolute',
                  transformStyle: 'preserve-3d',
                  zIndex: 100 - ax,
                  pointerEvents: ax > 3 ? 'none' : 'auto',
                }}
                className="w-[300px] rounded-2xl border border-border bg-card p-5 text-left shadow-lg"
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
                <div className="mt-5 font-display text-base font-bold tracking-tight text-foreground">
                  {r.turns} turns · {r.tool_calls} tools
                </div>
                <div className="mt-3 flex items-center justify-between border-t border-border pt-3 text-[11px] text-muted-foreground">
                  <span>{relativeTime(r.started_at)}</span>
                  {o === 0 && <span className="font-semibold text-primary">Open trace →</span>}
                </div>
              </motion.button>
            )
          })}
        </div>
      </div>
      <div className="flex items-center justify-center gap-4 border-t border-border py-3">
        <button
          type="button"
          onClick={() => go(center - 1)}
          className="grid size-9 place-items-center rounded-lg border border-border text-muted-foreground hover:text-foreground"
          aria-label="Previous"
        >
          <ChevronLeft className="size-4" />
        </button>
        <span className="min-w-16 text-center font-mono text-xs tabular-nums text-muted-foreground">
          {center + 1} / {runs.length}
        </span>
        <button
          type="button"
          onClick={() => go(center + 1)}
          className="grid size-9 place-items-center rounded-lg border border-border text-muted-foreground hover:text-foreground"
          aria-label="Next"
        >
          <ChevronRight className="size-4" />
        </button>
      </div>
    </div>
  )
}
