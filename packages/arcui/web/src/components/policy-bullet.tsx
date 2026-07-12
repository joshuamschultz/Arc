import type { PolicyBullet } from '@/lib/types'
import { cn } from '@/lib/utils'
import { relativeTime } from '@/lib/format'
import { scoreTier, TIER_META } from '@/lib/policy'

/** One ACE policy bullet. Shared by the Policy page and agent-detail.
 *  Scores are integers 1–10 (new=5, retired ≤2). */
export function PolicyBulletCard({ bullet }: { bullet: PolicyBullet }) {
  const score = typeof bullet.score === 'number' ? bullet.score : null
  const tier = scoreTier(bullet.score, bullet.retired)
  const meta = TIER_META[tier]
  const fill = score != null ? Math.max(4, Math.min(100, score * 10)) : 0

  return (
    <div className={cn('rounded-lg border border-border bg-card p-3 shadow-xs transition-colors duration-150 hover:border-border/80', bullet.retired && 'opacity-60')}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          {bullet.id && (
            <span className="mr-2 rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-primary">
              {bullet.id}
            </span>
          )}
          <span className="text-sm leading-relaxed text-foreground">{bullet.text || '(empty)'}</span>
        </div>
        {score != null && (
          <div className="flex shrink-0 items-center gap-2">
            <div className="h-1.5 w-16 overflow-hidden rounded-full bg-muted">
              <div className={cn('h-full rounded-full', meta.bar)} style={{ width: `${fill}%` }} />
            </div>
            <span className={cn('w-5 text-right text-xs font-semibold tabular-nums', meta.text)}>{score}</span>
          </div>
        )}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        {bullet.agent_id && <span className="font-mono">{bullet.agent_id}</span>}
        {typeof bullet.uses === 'number' && <span className="tabular-nums">{bullet.uses} uses</span>}
        {bullet.reviewed && <span>reviewed {relativeTime(bullet.reviewed)}</span>}
        {bullet.created && <span>created {relativeTime(bullet.created)}</span>}
        {bullet.source && <span className="font-mono">src {bullet.source}</span>}
        {bullet.retired && (
          <span className="rounded-full bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">retired</span>
        )}
      </div>
    </div>
  )
}
