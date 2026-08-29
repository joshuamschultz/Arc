import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { initials } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { AgentIdentityShape } from '@/lib/types'

export interface AgentIdentityProps {
  /** The canonical shape resolved server-side by arcui.identity (H-007). */
  identity: AgentIdentityShape
  /** Shown when the DID has no roster-joined `name` (e.g. a bare agent_id). */
  fallbackName?: string
  /** Avatar background — the roster's deterministic per-agent color. */
  color?: string
  /** Renders the online/offline dot next to the name when provided. */
  online?: boolean
  size?: 'sm' | 'md'
  showAvatar?: boolean
  className?: string
}

/**
 * The ONE renderer for an agent's identity across the dashboard (H-007).
 *
 * Friendly name is what a non-technical operator reads first; a compact
 * `type · host` line says what kind of agent this is and where it runs;
 * the DID stays visible as a short-id chip (tooltip reveals the full DID)
 * so the two never drift apart. Every screen that shows an agent should
 * render this component instead of re-deriving parts from a raw DID
 * string — the parsing already happened once, server-side, keyed by DID.
 */
export function AgentIdentity({
  identity,
  fallbackName,
  color,
  online,
  size = 'md',
  showAvatar = true,
  className,
}: AgentIdentityProps) {
  const label = identity.name || fallbackName || identity.did || 'Unknown agent'
  const metaParts = [identity.type, identity.host].filter(
    (part) => part && part !== 'unknown',
  )
  const chipText =
    identity.short_id && identity.short_id !== 'unknown' ? identity.short_id : identity.did || '—'

  const copyDid = () => {
    if (identity.did) void navigator.clipboard?.writeText(identity.did)
  }

  return (
    <div className={cn('flex min-w-0 items-center gap-3', className)}>
      {showAvatar && (
        <span
          className={cn(
            'flex shrink-0 items-center justify-center rounded-lg font-semibold text-primary-foreground',
            size === 'sm' ? 'size-7 text-xs' : 'size-9 text-sm',
          )}
          style={{ background: color || 'var(--primary)' }}
        >
          {initials(label)}
        </span>
      )}
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span
            className={cn(
              'truncate font-semibold tracking-tight text-foreground',
              size === 'sm' ? 'text-sm' : 'text-lg',
            )}
          >
            {label}
          </span>
          {online != null && (
            <span
              className={cn(
                'size-2 shrink-0 rounded-full',
                online ? 'bg-status-online' : 'bg-muted-foreground/40',
              )}
              aria-label={online ? 'online' : 'offline'}
            />
          )}
        </div>
        {metaParts.length > 0 && (
          <div className="truncate text-[11px] uppercase tracking-[0.06em] text-muted-foreground">
            {metaParts.join(' · ')}
          </div>
        )}
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={copyDid}
              title="Click to copy full DID"
              className="mt-1 inline-flex max-w-full cursor-pointer items-center truncate rounded border border-border bg-muted/40 px-1.5 py-0.5 align-middle font-mono text-[11px] text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
            >
              {chipText}
            </button>
          </TooltipTrigger>
          <TooltipContent className="font-mono text-[11px]">
            {identity.did || 'No DID reported'}
          </TooltipContent>
        </Tooltip>
      </div>
    </div>
  )
}
