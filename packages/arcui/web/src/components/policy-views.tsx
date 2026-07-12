import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { scoreDistribution, TIER_META, type ScoreTier } from '@/lib/policy'
import type { Dict, PolicyBullet } from '@/lib/types'

function Card({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4 shadow-xs">
      <div className="mb-3">
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
        {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
      </div>
      {children}
    </div>
  )
}

/** Uppercase micro-label table header row, shared by this file's tables. */
function THead({ children }: { children: ReactNode }) {
  return (
    <tr className="text-left text-[11px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
      {children}
    </tr>
  )
}

/** Mono chip for an ID/hash — matches the governance-surface convention. */
function IdChip({ children }: { children: ReactNode }) {
  return (
    <span className="rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-primary">
      {children}
    </span>
  )
}

/** Horizontal score-distribution bars across the four tiers. */
export function ScoreDistribution({ bullets }: { bullets: PolicyBullet[] }) {
  const d = scoreDistribution(bullets)
  const tiers: ScoreTier[] = ['high', 'mid', 'low', 'retired']
  return (
    <Card title="Score distribution" hint={`${d.total} bullets`}>
      <div className="space-y-2.5">
        {tiers.map((t) => {
          const count = d[t]
          const pct = d.total ? Math.round((count / d.total) * 100) : 0
          const meta = TIER_META[t]
          return (
            <div key={t}>
              <div className="mb-1 flex items-center justify-between text-xs">
                <span className="text-muted-foreground">{meta.label}</span>
                <span className={cn('tabular-nums', meta.text)}>{pct}% · {count}</span>
              </div>
              <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                <div className={cn('h-full rounded-full', meta.bar)} style={{ width: `${pct}%` }} />
              </div>
            </div>
          )
        })}
      </div>
    </Card>
  )
}

/** Top bullets by recorded use count. */
export function TopPerformers({ bullets, limit = 6 }: { bullets: PolicyBullet[]; limit?: number }) {
  const top = [...bullets]
    .filter((b) => (b.uses ?? 0) > 0)
    .sort((a, b) => (b.uses ?? 0) - (a.uses ?? 0))
    .slice(0, limit)
  return (
    <Card title="Top performers" hint="by uses">
      {top.length === 0 ? (
        <p className="text-xs text-muted-foreground">No usage recorded yet</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <THead>
              <th className="py-1.5">ID</th>
              <th className="py-1.5">Uses</th>
              <th className="py-1.5">Score</th>
              <th className="py-1.5">Bullet</th>
            </THead>
          </thead>
          <tbody>
            {top.map((b, i) => (
              <tr key={i} className="border-t border-border/60">
                <td className="py-1.5 pr-2">{b.id ? <IdChip>{b.id}</IdChip> : <span className="text-xs text-muted-foreground">—</span>}</td>
                <td className="py-1.5 pr-2 font-mono text-xs tabular-nums text-foreground">{b.uses ?? 0}</td>
                <td className="py-1.5 pr-2 font-mono text-xs tabular-nums text-foreground">{b.score ?? '—'}</td>
                <td className="max-w-0 truncate py-1.5 text-xs text-muted-foreground" title={b.text}>{b.text}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  )
}

function ConfigItem({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right font-mono text-foreground">{value ?? '—'}</span>
    </div>
  )
}

/** The policy engine's scoring/eval/security/evaluator config, parsed from
 *  the agent's arcagent.toml. */
export function PolicyConfigCards({ config }: { config: Dict }) {
  const pol = (((config.modules as Dict)?.policy as Dict)?.config ?? {}) as Dict
  const evalCfg = (config.eval ?? {}) as Dict
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <Card title="Scoring">
        <div className="space-y-1 text-xs">
          <ConfigItem label="New bullet" value="score 5" />
          <ConfigItem label="Hit" value={<span className="text-status-online">+1</span>} />
          <ConfigItem label="Miss" value={<span className="text-status-error">−2</span>} />
          <p className="pt-1 text-[11px] text-muted-foreground">Range 1–10, retired at ≤2</p>
        </div>
      </Card>
      <Card title="Eval triggers">
        <div className="space-y-1 text-xs">
          <ConfigItem label="Interval" value={pol.eval_interval_turns != null ? `${pol.eval_interval_turns} turns` : '—'} />
          <ConfigItem label="On" value="agent:shutdown" />
          <ConfigItem label="On" value="policy:force_eval" />
          <p className="pt-1 text-[11px] text-muted-foreground">Max {String(pol.max_bullets ?? '—')} bullets</p>
        </div>
      </Card>
      <Card title="Security (ASI-06)">
        <div className="space-y-1 text-xs">
          <ConfigItem label="Normalize" value="NFKC" />
          <ConfigItem label="Strip" value="zero-width" />
          <ConfigItem label="Remove" value="control chars" />
          <p className="pt-1 text-[11px] text-muted-foreground">Max {String(pol.max_bullet_text_length ?? '—')} chars</p>
        </div>
      </Card>
      <Card title="Evaluator">
        <div className="space-y-1 text-xs">
          <ConfigItem label="Model" value={String(evalCfg.model ?? '—')} />
          <ConfigItem label="Fallback" value={String(evalCfg.fallback_behavior ?? '—')} />
          <ConfigItem label="Concurrency" value={String(evalCfg.max_concurrent ?? '—')} />
          <p className="pt-1 text-[11px] text-muted-foreground">timeout {String(evalCfg.timeout_seconds ?? '—')}s</p>
        </div>
      </Card>
    </div>
  )
}

interface PolicyRule {
  id: string
  desc: string
  scope: string
  action: 'deny' | 'allow' | 'timeout' | 'allow-all'
  score: number
  status: 'active' | 'inactive'
}

const ACTION_TONE: Record<PolicyRule['action'], string> = {
  deny: 'bg-status-error/15 text-status-error',
  allow: 'bg-status-online/15 text-status-online',
  timeout: 'bg-status-warning/15 text-status-warning',
  'allow-all': 'bg-muted text-muted-foreground',
}

/** System rules derived from [tools.policy] — applied to every tool call. */
export function SystemPolicyRules({ config }: { config: Dict }) {
  const policy = ((config.tools as Dict)?.policy ?? {}) as Dict
  const deny = (policy.deny as string[]) ?? []
  const allow = (policy.allow as string[]) ?? []
  const timeout = policy.timeout_seconds
  const rules: PolicyRule[] = []
  deny.forEach((t, i) =>
    rules.push({ id: `POL-D${String(i + 1).padStart(3, '0')}`, desc: `Tool \`${t}\` is denied — registry skips on load`, scope: 'all calls', action: 'deny', score: 10, status: 'active' }),
  )
  allow.forEach((t, i) =>
    rules.push({ id: `POL-A${String(i + 1).padStart(3, '0')}`, desc: `Tool \`${t}\` explicitly allowed`, scope: 'all calls', action: 'allow', score: 10, status: 'active' }),
  )
  if (timeout != null)
    rules.push({ id: 'POL-T001', desc: `Tool calls aborted after ${timeout}s`, scope: 'all tools', action: 'timeout', score: 9, status: 'active' })
  if (rules.length === 0)
    rules.push({ id: 'POL-DEFAULT', desc: 'No tool policy configured — registry default applies', scope: 'all calls', action: 'allow-all', score: 5, status: 'inactive' })

  return (
    <Card title="System policy rules" hint="From [tools.policy] in arcagent.toml — apply to every call">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <THead>
              <th className="py-1.5 pr-3">Rule</th>
              <th className="py-1.5 pr-3">Description</th>
              <th className="py-1.5 pr-3">Scope</th>
              <th className="py-1.5 pr-3">Action</th>
              <th className="py-1.5 pr-3">Score</th>
              <th className="py-1.5">Status</th>
            </THead>
          </thead>
          <tbody>
            {rules.map((r) => (
              <tr key={r.id} className={cn('border-t border-border/60', r.status === 'inactive' && 'opacity-60')}>
                <td className="py-1.5 pr-3"><IdChip>{r.id}</IdChip></td>
                <td className="py-1.5 pr-3 text-xs text-muted-foreground">{r.desc}</td>
                <td className="py-1.5 pr-3 text-xs text-muted-foreground">{r.scope}</td>
                <td className="py-1.5 pr-3">
                  <span className={cn('rounded-full px-2 py-0.5 text-[11px] font-medium', ACTION_TONE[r.action])}>{r.action}</span>
                </td>
                <td className="py-1.5 pr-3 font-mono text-xs tabular-nums text-foreground">{r.score}</td>
                <td className="py-1.5 text-xs text-muted-foreground">{r.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}

/** Per-agent rollup for the fleet policy page. */
export function PerAgentBreakdown({ rows }: { rows: Dict[] }) {
  if (rows.length === 0) return null
  return (
    <Card title="Per-agent breakdown">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <THead>
              <th className="py-1.5 pr-3">Agent</th>
              <th className="py-1.5 pr-3">Total</th>
              <th className="py-1.5 pr-3">Active</th>
              <th className="py-1.5 pr-3">Retired</th>
              <th className="py-1.5">Avg score</th>
            </THead>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i} className="border-t border-border/60">
                <td className="py-1.5 pr-3"><IdChip>{String(r.agent_id ?? '—')}</IdChip></td>
                <td className="py-1.5 pr-3 font-mono text-xs tabular-nums text-foreground">{Number(r.total ?? 0)}</td>
                <td className="py-1.5 pr-3 font-mono text-xs tabular-nums text-foreground">{Number(r.active ?? 0)}</td>
                <td className="py-1.5 pr-3 font-mono text-xs tabular-nums text-foreground">{Number(r.retired ?? 0)}</td>
                <td className="py-1.5 font-mono text-xs tabular-nums text-foreground">{Number(r.avg_score ?? 0).toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}
