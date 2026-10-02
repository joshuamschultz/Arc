import { useState } from 'react'
import { Sparkles } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { ErrorState, LoadingRows } from '@/components/states'
import { UnifiedDiff } from '@/components/unified-diff'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import {
  useImproveSkill,
  useSkillImproverState,
  type GateVerdict,
  type ImproveResult,
  type ImproverCandidate,
  type GateLogEntry,
} from '@/lib/skill-controls'
import { ApiError } from '@/lib/api'
import { cn } from '@/lib/utils'

const shortId = (id: string) => (id === 'seed' ? 'seed' : id.slice(0, 8))
const formatScores = (scores: Record<string, number>) =>
  Object.entries(scores)
    .map(([dim, value]) => `${dim} ${value}`)
    .join(' · ')

/** The skill improver for one skill (alpha-2 P8): lifecycle state, usage evidence,
 *  candidates with judge scores, and recent eval-gate verdicts with their reasons.
 *  An operator can improve now: preview the proposed diff and gate verdict, then
 *  apply exactly that candidate (the agent re-runs the gate and authorization). */
export function SkillImproverCard({ agentId, skillName }: { agentId: string; skillName: string }) {
  const [operatorMode] = useOperatorMode()
  const state = useSkillImproverState(agentId, skillName)
  const improve = useImproveSkill(agentId, skillName)
  const [preview, setPreview] = useState<ImproveResult | null>(null)
  const [outcome, setOutcome] = useState<ImproveResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const run = async (dryRun: boolean) => {
    if (!dryRun && !window.confirm(`Apply this candidate to '${skillName}'?`)) return
    setError(null)
    setOutcome(null)
    try {
      const result = await improve.mutateAsync({ dryRun, previewId: preview?.preview_id })
      if (dryRun) setPreview(result)
      else {
        setPreview(null)
        setOutcome(result)
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Improve failed')
    }
  }

  if (state.isLoading) return <LoadingRows rows={5} />
  if (state.isError) return <ErrorState error={state.error} />
  const data = state.data
  if (!data) return null

  const canApply =
    preview?.status === 'preview' && preview.gate?.accepted === true && !!preview.preview_id

  return (
    <div className="space-y-4 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-foreground">
          {data.lifecycle_state}
        </span>
        <span className="text-muted-foreground">generation {data.generation}</span>
        <span className="text-muted-foreground">
          {data.traces.total} traces ({data.traces.success} ok, {data.traces.failure} failed)
        </span>
        <span className="text-muted-foreground">{data.suite.total} golden cases</span>
        {operatorMode && (
          <Button
            size="sm"
            className="ml-auto"
            disabled={!data.live || improve.isPending}
            title={data.live ? undefined : 'The agent is not running in this server.'}
            onClick={() => void run(true)}
          >
            <Sparkles className="size-3.5" /> {improve.isPending ? 'Working…' : 'Improve now'}
          </Button>
        )}
      </div>
      {data.lifecycle_reason && <p className="text-muted-foreground">{data.lifecycle_reason}</p>}
      {operatorMode && !data.live && (
        <p className="text-muted-foreground">
          Improve now needs the agent running in this server.
        </p>
      )}
      {error && <p className="text-destructive">{error}</p>}
      {outcome && <Outcome result={outcome} />}
      {preview && (
        <Preview result={preview} canApply={canApply} busy={improve.isPending} onApply={() => void run(false)} />
      )}

      <section className="space-y-1.5">
        <p className="font-medium text-foreground">Candidates</p>
        {data.candidates.length === 0 ? (
          <p className="text-muted-foreground">No candidates yet.</p>
        ) : (
          data.candidates.map((c) => <CandidateRow key={c.candidate_id} candidate={c} />)
        )}
      </section>

      <section className="space-y-1.5">
        <p className="font-medium text-foreground">Recent gate verdicts</p>
        {data.gate_log.length === 0 ? (
          <p className="text-muted-foreground">No improvement pass has run yet.</p>
        ) : (
          data.gate_log.map((entry, i) => <GateRow key={`${entry.ts}-${i}`} entry={entry} />)
        )}
      </section>
    </div>
  )
}

function Verdict({ gate }: { gate: GateVerdict }) {
  return (
    <div className="space-y-0.5">
      <p className={cn('font-medium', gate.accepted ? 'text-status-online' : 'text-destructive')}>
        Gate {gate.accepted ? 'accepted' : 'rejected'}
        {gate.before_pass != null && gate.after_pass != null &&
          ` · passing ${gate.before_pass} → ${gate.after_pass}`}
      </p>
      <p className="text-muted-foreground">{gate.reason}</p>
    </div>
  )
}

function Preview({
  result,
  canApply,
  busy,
  onApply,
}: {
  result: ImproveResult
  canApply: boolean
  busy: boolean
  onApply: () => void
}) {
  if (result.status !== 'preview') {
    return <p className="text-muted-foreground">{result.reason || result.status}</p>
  }
  return (
    <section className="space-y-2 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-start gap-3">
        <div className="flex-1">{result.gate && <Verdict gate={result.gate} />}</div>
        {canApply && (
          <Button size="sm" disabled={busy} onClick={onApply}>
            Apply
          </Button>
        )}
      </div>
      {result.scores && (
        <p className="text-muted-foreground">Judge scores: {formatScores(result.scores)}</p>
      )}
      {result.approval_required && (
        <p className="text-muted-foreground">Applying needs operator approval at this tier.</p>
      )}
      <UnifiedDiff diff={result.diff ?? ''} emptyText="The candidate is identical to the current skill." />
    </section>
  )
}

function Outcome({ result }: { result: ImproveResult }) {
  const applied = result.status === 'applied'
  return (
    <div className="space-y-1">
      <p className={cn('font-medium', applied ? 'text-status-online' : 'text-destructive')}>
        {applied ? `Applied candidate ${shortId(result.candidate_id ?? '')}` : `Not applied (${result.status})`}
      </p>
      <p className="text-muted-foreground">{result.reason}</p>
    </div>
  )
}

function CandidateRow({ candidate }: { candidate: ImproverCandidate }) {
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-border px-3 py-2">
      <span className="font-mono text-[11px] text-foreground">{shortId(candidate.candidate_id)}</span>
      {candidate.active && (
        <span className="rounded bg-status-online/15 px-1.5 py-0.5 text-[10px] text-status-online">active</span>
      )}
      <span className="text-muted-foreground">gen {candidate.generation}</span>
      {Object.keys(candidate.scores).length > 0 && (
        <span className="text-muted-foreground">{formatScores(candidate.scores)}</span>
      )}
    </div>
  )
}

function GateRow({ entry }: { entry: GateLogEntry }) {
  return (
    <div className="space-y-0.5 rounded-md border border-border px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={cn(
            'rounded px-1.5 py-0.5 text-[10px]',
            entry.outcome === 'applied'
              ? 'bg-status-online/15 text-status-online'
              : 'bg-destructive/15 text-destructive',
          )}
        >
          {entry.outcome}
        </span>
        <span className="text-muted-foreground">
          {entry.source} · {entry.kind}
        </span>
        {entry.ts && <span className="ml-auto text-muted-foreground">{entry.ts}</span>}
      </div>
      <p className="text-muted-foreground">{entry.reason}</p>
    </div>
  )
}
