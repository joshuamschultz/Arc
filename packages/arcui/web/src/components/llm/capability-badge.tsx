import { cn } from '@/lib/utils'

const TONE: Record<string, string> = {
  inference: 'bg-primary/10 text-primary border-primary/25',
  embedding: 'bg-chart-2/12 text-chart-2 border-chart-2/30',
}

/**
 * What an LLM call WAS — "inference" (chat/completion) or "embedding" —
 * classified server-side from the recorded call type, never guessed from
 * the model name (H-028). Absent/unknown reads as inference: every call
 * that isn't a stamped embed IS a completion call.
 */
export function CapabilityBadge({ value }: { value?: string | null }) {
  const key = value || 'inference'
  const cls = TONE[key] ?? 'border-border bg-muted text-muted-foreground'
  return (
    <span
      className={cn(
        'inline-flex w-fit items-center rounded-md border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
        cls,
      )}
    >
      {key}
    </span>
  )
}
