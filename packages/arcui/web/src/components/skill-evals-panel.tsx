import { useState, type FormEvent } from 'react'
import { Play, RefreshCw, Star } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { ErrorState, LoadingRows } from '@/components/states'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { useAgentSkillEvals } from '@/lib/queries'
import {
  rubricDigest,
  usePromoteGolden,
  useRegenSkillEvals,
  useRunSkillEvals,
  type EvalCaseResult,
  type EvalsResult,
  type PromoteGoldenSpec,
} from '@/lib/skill-controls'
import { ApiError } from '@/lib/api'
import { cn } from '@/lib/utils'

const errorText = (e: unknown, fallback: string) => (e instanceof ApiError ? e.message : fallback)

/** The golden suite of one skill (alpha-2 P8): every case with its provenance and gate
 *  type; for an operator, run the suite now (pass/fail per case), regenerate the
 *  machine-authored anchors, and promote a new case to golden. */
export function SkillEvalsPanel({ agentId, skillName }: { agentId: string; skillName: string }) {
  const [operatorMode] = useOperatorMode()
  const evals = useAgentSkillEvals(agentId, skillName)
  const run = useRunSkillEvals(agentId, skillName)
  const regen = useRegenSkillEvals(agentId, skillName)
  const [error, setError] = useState<string | null>(null)

  const doRun = async () => {
    setError(null)
    try {
      await run.mutateAsync()
    } catch (e) {
      setError(errorText(e, 'Suite run failed'))
    }
  }

  const doRegen = async () => {
    if (!window.confirm(`Regenerate the machine-authored golden cases of '${skillName}'?`)) return
    setError(null)
    try {
      await regen.mutateAsync()
    } catch (e) {
      setError(errorText(e, 'Regeneration failed'))
    }
  }

  if (evals.isLoading) return <LoadingRows rows={4} />
  if (evals.isError) return <ErrorState error={evals.error} />

  const items = evals.data?.items ?? []
  const results = new Map((run.data?.cases ?? []).map((c) => [c.case_id, c]))

  return (
    <div className="space-y-4">
      {operatorMode && (
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" disabled={run.isPending || items.length === 0} onClick={() => void doRun()}>
            <Play className="size-3.5" /> {run.isPending ? 'Running…' : 'Run suite'}
          </Button>
          <Button variant="outline" size="sm" disabled={regen.isPending} onClick={() => void doRegen()}>
            <RefreshCw className="size-3.5" /> {regen.isPending ? 'Regenerating…' : 'Regenerate'}
          </Button>
        </div>
      )}
      {error && <p className="text-xs text-destructive">{error}</p>}
      {run.data && <RunSummary result={run.data} />}
      {regen.data && <RegenSummary result={regen.data} />}

      {items.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          No golden cases yet. The improver generates them once the skill is used, or promote
          one below.
        </p>
      ) : (
        <ul className="space-y-1.5">
          {items.map((item) => (
            <CaseRow
              key={item.nodeid}
              nodeid={item.nodeid}
              provenance={item.provenance}
              gateType={item.gate_type}
              result={results.get(item.nodeid)}
            />
          ))}
        </ul>
      )}

      {operatorMode && <PromoteForm agentId={agentId} skillName={skillName} />}
    </div>
  )
}

function RunSummary({ result }: { result: EvalsResult }) {
  if (result.status !== 'completed') {
    return <p className="text-xs text-muted-foreground">{result.reason || result.status}</p>
  }
  return (
    <p
      className={cn(
        'text-xs font-medium',
        result.failed ? 'text-destructive' : 'text-status-online',
      )}
    >
      {result.passed} of {result.total} passed
    </p>
  )
}

function RegenSummary({ result }: { result: EvalsResult }) {
  const text =
    result.status === 'completed'
      ? `Adopted ${result.adopted ?? 0} new golden case(s); the suite has ${result.total ?? 0}.`
      : result.reason || result.status
  return <p className="text-xs text-muted-foreground">{text}</p>
}

function CaseRow({
  nodeid,
  provenance,
  gateType,
  result,
}: {
  nodeid: string
  provenance: string
  gateType: string
  result?: EvalCaseResult
}) {
  return (
    <li className="flex flex-wrap items-center gap-2 rounded-md border border-border px-3 py-2 text-xs">
      <span className="font-mono text-[11px] text-foreground">{nodeid}</span>
      <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">{provenance}</span>
      <span className="text-[10px] text-muted-foreground">{gateType}</span>
      {result && (
        <span
          className={cn(
            'ml-auto rounded px-1.5 py-0.5 text-[10px]',
            result.passed ? 'bg-status-online/15 text-status-online' : 'bg-destructive/15 text-destructive',
          )}
        >
          {result.passed ? 'pass' : 'fail'}
        </span>
      )}
      {result && !result.passed && result.detail && (
        <span className="w-full font-mono text-[10px] text-destructive">{result.detail}</span>
      )}
    </li>
  )
}

/** Promote-to-golden: an operator-written case, signed and activated by the existing
 *  curation route. exact_match pins an ideal output; judge_rubric pins a rubric and
 *  its judge model (the rubric's sha256 is computed here so the pin matches). */
function PromoteForm({ agentId, skillName }: { agentId: string; skillName: string }) {
  const promote = usePromoteGolden(agentId, skillName)
  const [caseId, setCaseId] = useState('')
  const [gateType, setGateType] = useState<PromoteGoldenSpec['gate_type']>('exact_match')
  const [ideal, setIdeal] = useState('')
  const [rubric, setRubric] = useState('')
  const [judge, setJudge] = useState('')
  const [error, setError] = useState<string | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    try {
      const spec: PromoteGoldenSpec =
        gateType === 'exact_match'
          ? { case_id: caseId, gate_type: gateType, ideal_output: ideal }
          : {
              case_id: caseId,
              gate_type: gateType,
              rubric,
              judge_model_id: judge,
              rubric_sha256: await rubricDigest(rubric),
            }
      await promote.mutateAsync(spec)
      setCaseId('')
      setIdeal('')
      setRubric('')
    } catch (e) {
      setError(errorText(e, 'Promote failed'))
    }
  }

  const ready = caseId.trim() && (gateType === 'exact_match' ? ideal.trim() : rubric.trim() && judge.trim())

  return (
    <form onSubmit={(e) => void submit(e)} className="space-y-2 rounded-md border border-border p-3">
      <p className="text-xs font-medium text-foreground">Promote a case to golden</p>
      <div className="flex flex-wrap gap-2">
        <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
          Case id
          <Input aria-label="Case id" value={caseId} onChange={(e) => setCaseId(e.target.value)} className="h-8 w-48 text-xs" />
        </label>
        <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
          Gate
          <select
            aria-label="Gate"
            value={gateType}
            onChange={(e) => setGateType(e.target.value as PromoteGoldenSpec['gate_type'])}
            className="h-8 rounded-md border border-input bg-transparent px-2 text-xs text-foreground"
          >
            <option value="exact_match">exact match</option>
            <option value="judge_rubric">judge rubric</option>
          </select>
        </label>
      </div>
      {gateType === 'exact_match' ? (
        <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
          Ideal output
          <Textarea aria-label="Ideal output" value={ideal} onChange={(e) => setIdeal(e.target.value)} className="min-h-16 text-xs" />
        </label>
      ) : (
        <>
          <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
            Rubric
            <Textarea aria-label="Rubric" value={rubric} onChange={(e) => setRubric(e.target.value)} className="min-h-16 text-xs" />
          </label>
          <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
            Judge model
            <Input aria-label="Judge model" value={judge} onChange={(e) => setJudge(e.target.value)} placeholder="provider/model" className="h-8 text-xs" />
          </label>
        </>
      )}
      {error && <p className="text-xs text-destructive">{error}</p>}
      {promote.data && (
        <p className="text-xs text-status-online">Promoted {promote.data.nodeid}</p>
      )}
      <Button type="submit" size="sm" disabled={!ready || promote.isPending}>
        <Star className="size-3.5" /> {promote.isPending ? 'Promoting…' : 'Promote to golden'}
      </Button>
    </form>
  )
}
