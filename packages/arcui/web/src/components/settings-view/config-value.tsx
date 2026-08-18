import type { Dict } from '@/lib/types'

// Read-only rendering of a config (sub-)value. Scalars read as a clean, zebra
// key/value list; nested tables recurse into their own labeled block so deep
// nesting (modules → memory → config → dynamics) stays scannable instead of
// collapsing into one thin rail.

function isPlainObject(value: unknown): value is Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function ReadValue({ value }: { value: unknown }) {
  if (value === null || value === undefined)
    return <span className="text-muted-foreground">—</span>
  if (typeof value === 'boolean')
    return (
      <span
        className={
          value
            ? 'font-mono text-xs font-medium text-status-online'
            : 'font-mono text-xs font-medium text-muted-foreground'
        }
      >
        {String(value)}
      </span>
    )
  if (Array.isArray(value)) {
    return value.length === 0 ? (
      <span className="text-muted-foreground">empty</span>
    ) : (
      <span className="font-mono text-xs text-foreground">
        {value.map((v) => String(v)).join(', ')}
      </span>
    )
  }
  if (isPlainObject(value)) return <ConfigTree obj={value} />
  return <span className="font-mono text-xs break-words text-foreground">{String(value)}</span>
}

export function ConfigTree({ obj }: { obj: Dict }) {
  const entries = Object.entries(obj)
  if (entries.length === 0) return <span className="text-muted-foreground">empty</span>

  const scalars = entries.filter(([, v]) => !isPlainObject(v))
  const objects = entries.filter(([, v]) => isPlainObject(v))

  return (
    <div className="space-y-3">
      {scalars.length > 0 && (
        <div className="overflow-hidden rounded-md border border-border/60">
          {scalars.map(([k, v], i) => (
            <div
              key={k}
              className={
                'grid grid-cols-[minmax(6rem,auto)_minmax(0,1fr)] items-baseline gap-x-4 px-2.5 py-1.5 ' +
                (i % 2 === 1 ? 'bg-muted/20' : '')
              }
            >
              <span className="font-mono text-[11px] text-muted-foreground">{k}</span>
              <div className="min-w-0 text-left">
                <ReadValue value={v} />
              </div>
            </div>
          ))}
        </div>
      )}
      {objects.map(([k, v]) => (
        <div key={k} className="rounded-md border border-border/60 bg-muted/20 p-2.5">
          <div className="mb-2 inline-flex rounded border border-border bg-background px-1.5 py-0.5 font-mono text-[11px] font-semibold text-foreground">
            {k}
          </div>
          <ConfigTree obj={v as Dict} />
        </div>
      ))}
    </div>
  )
}
