import type { ReactNode } from 'react'
import { AlertCircle, Inbox } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { ApiError } from '@/lib/api'

/** Centered empty placeholder for a section with no data. */
export function EmptyState({
  icon,
  title,
  description,
}: {
  icon?: ReactNode
  title: string
  description?: string
}) {
  return (
    <div
      className="relative flex flex-col items-center justify-center gap-3 overflow-hidden rounded-lg border border-border bg-card/40 px-8 py-12 text-center"
      style={{
        backgroundImage:
          'radial-gradient(color-mix(in oklch, var(--muted-foreground) 22%, transparent) 1px, transparent 1px)',
        backgroundSize: '16px 16px',
      }}
    >
      <span className="flex size-12 items-center justify-center rounded-full border border-border bg-muted/40 text-muted-foreground [&>svg]:size-5">
        {icon ?? <Inbox className="size-5" />}
      </span>
      <div className="text-sm font-medium text-foreground">{title}</div>
      {description && (
        <p className="max-w-sm text-xs text-muted-foreground">{description}</p>
      )}
    </div>
  )
}

export function ErrorState({ error }: { error: unknown }) {
  const msg =
    error instanceof ApiError
      ? `${error.message}${error.status === 401 ? ' — check your token' : ''}`
      : error instanceof Error
        ? error.message
        : 'Request failed'
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-lg border border-destructive/30 bg-destructive/10 px-8 py-10 text-center">
      <span className="flex size-12 items-center justify-center rounded-full border border-destructive/30 bg-destructive/10 text-destructive">
        <AlertCircle className="size-5" />
      </span>
      <div className="text-sm font-medium text-foreground">Couldn’t load data</div>
      <p className="max-w-sm text-xs text-muted-foreground">{msg}</p>
    </div>
  )
}

export function LoadingRows({ rows = 6 }: { rows?: number }) {
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }).map((_, i) => (
        <Skeleton key={i} className="h-10 w-full rounded-lg" />
      ))}
    </div>
  )
}

/**
 * Standard loading/error/empty wrapper for a react-query result. Renders
 * `children(data)` only once data is present and non-empty.
 */
export function QueryState<T>({
  query,
  isEmpty,
  empty,
  children,
}: {
  query: { isLoading: boolean; isError: boolean; error: unknown; data?: T }
  isEmpty?: (data: T) => boolean
  empty?: ReactNode
  children: (data: T) => ReactNode
}) {
  if (query.isLoading) return <LoadingRows />
  if (query.isError) return <ErrorState error={query.error} />
  if (query.data === undefined) return <LoadingRows />
  if (isEmpty?.(query.data)) return <>{empty ?? <EmptyState title="Nothing here yet" />}</>
  return <>{children(query.data)}</>
}
