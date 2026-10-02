// alpha-2 P8 — arcskill operator controls: improver read model, improve-now
// (preview → apply), golden-suite run / regen, and promote-to-golden.
// Server: arcui/routes/agent_detail/skill_improver.py (+ skill_versions.py /promote).

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiGet, apiPost } from '@/lib/api'

/** The server bounds each control at the improver's `manual_timeout_s` (600s by
 *  default); the browser waits a little longer than that before giving up. */
const CONTROL_TIMEOUT_MS = 11 * 60_000

export interface GateVerdict {
  accepted: boolean
  reason: string
  before_pass?: number
  after_pass?: number
  newly_passing?: number
}

export interface GateLogEntry extends GateVerdict {
  skill_name: string
  source: 'auto' | 'manual' | string
  kind: string
  outcome: 'applied' | 'rejected' | 'denied' | string
  candidate_id: string
  ts: string
}

export interface ImproverCandidate {
  candidate_id: string
  generation: number
  parent_id: string | null
  scores: Record<string, number>
  active: boolean
  in_frontier: boolean
}

export interface ImproverState {
  skill_name: string
  lifecycle_state: string
  active_candidate_id: string | null
  generation: number
  lifecycle_reason: string
  merged_into: string | null
  traces: { total: number; success: number; failure: number }
  suite: { total: number; human: number; machine: number; curated: number }
  candidates: ImproverCandidate[]
  gate_log: GateLogEntry[]
  /** True when the agent runs in this server process (controls are reachable). */
  live: boolean
}

export interface ImproveResult {
  status: string
  skill_name: string
  reason: string
  preview_id?: string
  candidate_id?: string
  generation?: number
  diff?: string
  scores?: Record<string, number>
  seed_scores?: Record<string, number>
  gate?: GateVerdict
  approval_required?: boolean
}

export interface EvalCaseResult {
  case_id: string
  passed: boolean
  detail: string
  gate_type: string
  provenance: string
}

export interface EvalsResult {
  status: string
  skill_name: string
  reason: string
  total?: number
  passed?: number
  failed?: number
  cases?: EvalCaseResult[]
  adopted?: number
  quarantined?: { nodeid: string; reason: string }[]
}

export interface PromoteGoldenSpec {
  case_id: string
  gate_type: 'exact_match' | 'judge_rubric'
  ideal_output?: string
  rubric?: string
  judge_model_id?: string
  rubric_sha256?: string
}

const skillBase = (agentId: string, skillName: string) =>
  `/api/agents/${encodeURIComponent(agentId)}/skills/${encodeURIComponent(skillName)}`

export const useSkillImproverState = (agentId: string, skillName: string | null) =>
  useQuery<ImproverState>({
    queryKey: ['agent', agentId, 'skill', skillName, 'improver'],
    queryFn: ({ signal }) => apiGet(`${skillBase(agentId, skillName!)}/improver`, signal),
    enabled: !!skillName,
  })

/** Improve now. `dryRun` previews (diff + gate verdict, no write); otherwise applies,
 *  the previewed candidate when `previewId` is given. */
export const useImproveSkill = (agentId: string, skillName: string) => {
  const client = useQueryClient()
  return useMutation<ImproveResult, Error, { dryRun: boolean; previewId?: string }>({
    mutationFn: ({ dryRun, previewId }) =>
      apiPost(
        `${skillBase(agentId, skillName)}/improve?dry_run=${dryRun ? 1 : 0}`,
        dryRun ? {} : { confirm: true, preview_id: previewId },
        undefined,
        CONTROL_TIMEOUT_MS,
      ),
    onSuccess: (_result, { dryRun }) => {
      if (dryRun) return
      return Promise.all([
        client.invalidateQueries({ queryKey: ['agent', agentId, 'skill', skillName] }),
      ]).then(() => undefined)
    },
  })
}

export const useRunSkillEvals = (agentId: string, skillName: string) =>
  useMutation<EvalsResult, Error, void>({
    mutationFn: () =>
      apiPost(`${skillBase(agentId, skillName)}/evals/run`, {}, undefined, CONTROL_TIMEOUT_MS),
  })

export const useRegenSkillEvals = (agentId: string, skillName: string) => {
  const client = useQueryClient()
  return useMutation<EvalsResult, Error, void>({
    mutationFn: () =>
      apiPost(
        `${skillBase(agentId, skillName)}/evals/regen`,
        { confirm: true },
        undefined,
        CONTROL_TIMEOUT_MS,
      ),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ['agent', agentId, 'skill', skillName, 'evals'] }),
  })
}

/** Promote to golden — the existing signed curation route (`POST .../promote`). */
export const usePromoteGolden = (agentId: string, skillName: string) => {
  const client = useQueryClient()
  return useMutation<{ nodeid: string; gate_type: string }, Error, PromoteGoldenSpec>({
    mutationFn: (spec) => apiPost(`${skillBase(agentId, skillName)}/promote`, spec),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ['agent', agentId, 'skill', skillName, 'evals'] }),
  })
}

/** sha256 hex of the exact rubric text — the pin a judge_rubric case must carry. */
export async function rubricDigest(rubric: string): Promise<string> {
  const bytes = new TextEncoder().encode(rubric)
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}
