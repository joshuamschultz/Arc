import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SkillImproverCard } from '@/components/skill-improver-card'
import { apiGet, apiPost } from '@/lib/api'

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}))
const operator = { on: true }
vi.mock('@/hooks/use-operator-mode', () => ({ useOperatorMode: () => [operator.on, vi.fn()] }))

const STATE = {
  skill_name: 'reporter',
  lifecycle_state: 'active',
  active_candidate_id: 'abc123def456',
  generation: 1,
  lifecycle_reason: '',
  merged_into: null,
  traces: { total: 41, success: 30, failure: 11 },
  suite: { total: 3, human: 1, machine: 2, curated: 0 },
  candidates: [
    { candidate_id: 'abc123def456', generation: 1, parent_id: 'seed', scores: { accuracy: 4.5 }, active: true, in_frontier: true },
  ],
  gate_log: [
    {
      skill_name: 'reporter', source: 'auto', kind: 'prose', accepted: false,
      reason: 'regression on 1 golden case(s)', outcome: 'rejected', candidate_id: 'fff000',
      before_pass: 2, after_pass: 1, newly_passing: 0, ts: '2026-10-01T00:00:00+00:00',
    },
  ],
  live: true,
}
const PREVIEW = {
  status: 'preview',
  skill_name: 'reporter',
  reason: 'strict improvement: fixed failing case(s), no regression',
  preview_id: 'p'.repeat(32),
  candidate_id: 'beef00112233',
  generation: 2,
  diff: '--- current/SKILL.md\n+++ candidate/SKILL.md\n-old line\n+new line\n',
  scores: { accuracy: 4.8 },
  gate: { accepted: true, reason: 'strict improvement: fixed failing case(s), no regression', before_pass: 2, after_pass: 3, newly_passing: 1 },
  approval_required: false,
}

beforeEach(() => {
  operator.on = true
  vi.mocked(apiGet).mockResolvedValue(STATE)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.restoreAllMocks()
})

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <SkillImproverCard agentId="ada" skillName="reporter" />
    </QueryClientProvider>,
  )
}

it('shows the improver state, candidates with scores, and the last gate verdict', async () => {
  renderCard()
  expect((await screen.findAllByText('active')).length).toBe(2) // lifecycle + active candidate
  expect(screen.getByText(/41 traces/)).toBeTruthy()
  expect(screen.getByText('abc123de')).toBeTruthy()
  expect(screen.getByText(/accuracy 4\.5/)).toBeTruthy()
  expect(screen.getByText('regression on 1 golden case(s)')).toBeTruthy()
  expect(vi.mocked(apiGet)).toHaveBeenCalledWith('/api/agents/ada/skills/reporter/improver', expect.anything())
})

it('improve now previews the diff and gate verdict, then applies that preview', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  vi.mocked(apiPost)
    .mockResolvedValueOnce(PREVIEW)
    .mockResolvedValueOnce({ status: 'applied', skill_name: 'reporter', reason: 'ok', candidate_id: 'beef00112233' })
  renderCard()
  await userEvent.click(await screen.findByRole('button', { name: /improve now/i }))
  expect(await screen.findByText('+new line')).toBeTruthy()
  expect(screen.getByText(/gate accepted/i)).toBeTruthy()
  expect(vi.mocked(apiPost).mock.calls[0].slice(0, 2)).toEqual([
    '/api/agents/ada/skills/reporter/improve?dry_run=1',
    {},
  ])
  await userEvent.click(screen.getByRole('button', { name: /^apply$/i }))
  await screen.findByText(/applied/i)
  expect(vi.mocked(apiPost).mock.calls[1].slice(0, 2)).toEqual([
    '/api/agents/ada/skills/reporter/improve?dry_run=0',
    { confirm: true, preview_id: 'p'.repeat(32) },
  ])
})

it('a preview the gate rejected cannot be applied', async () => {
  vi.mocked(apiPost).mockResolvedValueOnce({
    ...PREVIEW,
    gate: { accepted: false, reason: 'regression on 2 golden case(s)' },
  })
  renderCard()
  await userEvent.click(await screen.findByRole('button', { name: /improve now/i }))
  expect(await screen.findByText(/gate rejected/i)).toBeTruthy()
  expect(screen.getByText('regression on 2 golden case(s)')).toBeTruthy()
  expect(screen.queryByRole('button', { name: /^apply$/i })).toBeNull()
})

it('a refusal (for example too few traces) is shown, not a diff', async () => {
  vi.mocked(apiPost).mockResolvedValueOnce({
    status: 'insufficient_traces', skill_name: 'reporter', reason: 'needs at least 2 usage traces (has 0)',
  })
  renderCard()
  await userEvent.click(await screen.findByRole('button', { name: /improve now/i }))
  expect(await screen.findByText('needs at least 2 usage traces (has 0)')).toBeTruthy()
  expect(screen.queryByRole('button', { name: /^apply$/i })).toBeNull()
})

it('improve now is off when the agent is not running in this server', async () => {
  vi.mocked(apiGet).mockResolvedValue({ ...STATE, live: false })
  renderCard()
  const button = await screen.findByRole('button', { name: /improve now/i })
  expect((button as HTMLButtonElement).disabled).toBe(true)
})

it('a viewer sees the state but no improve control', async () => {
  operator.on = false
  renderCard()
  await screen.findByText(/41 traces/)
  expect(screen.queryByRole('button', { name: /improve now/i })).toBeNull()
})
