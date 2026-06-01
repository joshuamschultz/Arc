import { cn } from '@/lib/utils'

/** Pretty-printed, scrollable JSON for raw payloads + the event drawer. */
export function JsonBlock({
  value,
  className,
}: {
  value: unknown
  className?: string
}) {
  let text: string
  try {
    text = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  } catch {
    text = String(value)
  }
  return (
    <pre
      className={cn(
        'overflow-auto rounded-lg border border-border bg-muted/30 p-3 font-mono text-xs leading-relaxed text-foreground',
        className,
      )}
    >
      {text}
    </pre>
  )
}
