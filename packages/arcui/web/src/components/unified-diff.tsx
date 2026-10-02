import { cn } from '@/lib/utils'

/** Renders a server-computed unified diff with +/- line coloring (plain text parse —
 *  no diff library needed for a format this simple). */
export function UnifiedDiff({ diff, emptyText }: { diff: string; emptyText?: string }) {
  if (!diff.trim()) {
    return (
      <p className="text-xs text-muted-foreground">
        {emptyText ?? 'No textual difference between these two versions.'}
      </p>
    )
  }
  return (
    <pre className="overflow-x-auto rounded-md border border-border bg-muted/20 p-3 font-mono text-[11px] leading-relaxed">
      {diff.split('\n').map((line, i) => (
        <div
          key={i}
          className={cn(
            line.startsWith('+') && !line.startsWith('+++') && 'bg-status-online/10 text-status-online',
            line.startsWith('-') && !line.startsWith('---') && 'bg-destructive/10 text-destructive',
            (line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@')) &&
              'text-muted-foreground',
          )}
        >
          {line || ' '}
        </div>
      ))}
    </pre>
  )
}
