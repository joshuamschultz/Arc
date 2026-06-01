import type { PolicyBullet } from '@/lib/types'
import { cn } from '@/lib/utils'
import { relativeTime } from '@/lib/format'

/** One ACE policy bullet. Shared by the Policy page and agent-detail. */
export function PolicyBulletCard({ bullet }: { bullet: PolicyBullet }) {
  const score = typeof bullet.score === 'number' ? bullet.score : null
  return (
    <div
      className={cn(
        'rounded-lg border border-border bg-card p-3 shadow-xs',
        bullet.retired && 'opacity-60',
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm leading-relaxed text-foreground">
          {bullet.text || '(empty)'}
        </p>
        {score != null && (
          <span
            className={cn(
              'shrink-0 rounded-md border px-1.5 py-0.5 text-xs font-semibold tabular-nums',
              score >= 0.5
                ? 'border-status-online/30 bg-status-online/10 text-status-online'
                : score < 0
                  ? 'border-status-error/30 bg-status-error/10 text-status-error'
                  : 'border-border bg-muted text-muted-foreground',
            )}
          >
            {score.toFixed(2)}
          </span>
        )}
      </div>
      <div className="mt-2 flex items-center gap-3 text-xs text-muted-foreground">
        {bullet.agent_id && <span className="font-mono">{bullet.agent_id}</span>}
        {typeof bullet.uses === 'number' && <span>{bullet.uses} uses</span>}
        {bullet.created && <span>{relativeTime(bullet.created)}</span>}
        {bullet.retired && (
          <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] font-medium">
            retired
          </span>
        )}
      </div>
    </div>
  )
}
