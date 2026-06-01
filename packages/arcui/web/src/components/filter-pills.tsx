import { cn } from '@/lib/utils'

export interface PillOption {
  value: string
  label: string
  count?: number
}

/** Segmented filter control used by Tasks, Security, etc. */
export function FilterPills({
  options,
  value,
  onChange,
}: {
  options: PillOption[]
  value: string
  onChange: (value: string) => void
}) {
  return (
    <div className="inline-flex flex-wrap gap-1 rounded-lg border border-border bg-card p-1">
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          onClick={() => onChange(opt.value)}
          className={cn(
            'rounded-md px-2.5 py-1 text-xs font-medium transition-colors',
            value === opt.value
              ? 'bg-primary/15 text-foreground'
              : 'text-muted-foreground hover:bg-muted/50 hover:text-foreground',
          )}
        >
          {opt.label}
          {typeof opt.count === 'number' && (
            <span className="ml-1.5 tabular-nums opacity-60">{opt.count}</span>
          )}
        </button>
      ))}
    </div>
  )
}
