import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SkillEvalsPanel } from '@/components/skill-evals-panel'
import { apiGet, apiPost } from '@/lib/api'

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}))
const operator = { on: true }
vi.mock('@/hooks/use-operator-mode', () => ({ useOperatorMode: () => [operator.on, vi.fn()] }))

const CASES = {
  items: [
    { nodeid: 'evals/test_g.py::test_a', provenance: 'human', gate_type: 'exact_match' },
    { nodeid: 'evals/test_golden_generated.py::test_b', provenance: 'machine', gate_type: 'exact_match' },
  ],
}

beforeEach(() => {
  operator.on = true
  vi.mocked(apiGet).mockResolvedValue(CASES)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.restoreAllMocks()
})

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <SkillEvalsPanel agentId="ada" skillName="reporter" />
    </QueryClientProvider>,
  )
  return client
}

it('lists every golden case with its provenance and gate type', async () => {
  renderPanel()
  expect(await screen.findByText('evals/test_g.py::test_a')).toBeTruthy()
  expect(screen.getByText('evals/test_golden_generated.py::test_b')).toBeTruthy()
  expect(screen.getByText('human')).toBeTruthy()
  expect(screen.getByText('machine')).toBeTruthy()
  expect(vi.mocked(apiGet)).toHaveBeenCalledWith('/api/agents/ada/skills/reporter/evals', expect.anything())
})

it('runs the suite and shows pass or fail for each case', async () => {
  vi.mocked(apiPost).mockResolvedValue({
    status: 'completed', skill_name: 'reporter', reason: '', total: 2, passed: 1, failed: 1,
    cases: [
      { case_id: 'evals/test_g.py::test_a', passed: true, detail: '', gate_type: 'exact_match', provenance: 'human' },
      { case_id: 'evals/test_golden_generated.py::test_b', passed: false, detail: 'AssertionError', gate_type: 'exact_match', provenance: 'machine' },
    ],
  })
  renderPanel()
  await userEvent.click(await screen.findByRole('button', { name: /run suite/i }))
  await screen.findByText('1 of 2 passed')
  const passRow = screen.getByText('evals/test_g.py::test_a').closest('li')!
  const failRow = screen.getByText('evals/test_golden_generated.py::test_b').closest('li')!
  expect(within(passRow).getByText('pass')).toBeTruthy()
  expect(within(failRow).getByText('fail')).toBeTruthy()
  expect(within(failRow).getByText('AssertionError')).toBeTruthy()
  expect(vi.mocked(apiPost).mock.calls[0][0]).toBe('/api/agents/ada/skills/reporter/evals/run')
})

it('regenerates the machine suite only after confirmation', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
  vi.mocked(apiPost).mockResolvedValue({
    status: 'completed', skill_name: 'reporter', reason: '', total: 4, adopted: 3,
  })
  renderPanel()
  await userEvent.click(await screen.findByRole('button', { name: /regenerate/i }))
  await screen.findByText(/adopted 3/i)
  expect(confirm).toHaveBeenCalled()
  expect(vi.mocked(apiPost).mock.calls[0].slice(0, 2)).toEqual([
    '/api/agents/ada/skills/reporter/evals/regen',
    { confirm: true },
  ])
})

it('does not regenerate when the operator cancels', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(false)
  renderPanel()
  await userEvent.click(await screen.findByRole('button', { name: /regenerate/i }))
  expect(vi.mocked(apiPost)).not.toHaveBeenCalled()
})

it('promotes a case to golden through the signed promote route', async () => {
  vi.mocked(apiPost).mockResolvedValue({ status: 'emitted', nodeid: 'evals/curated/test_inv.py::test_inv', gate_type: 'exact_match' })
  renderPanel()
  await screen.findByText('evals/test_g.py::test_a')
  await userEvent.type(screen.getByLabelText('Case id'), 'inv')
  await userEvent.type(screen.getByLabelText('Ideal output'), 'Acme $42')
  await userEvent.click(screen.getByRole('button', { name: /promote to golden/i }))
  await screen.findByText(/evals\/curated\/test_inv.py::test_inv/)
  expect(vi.mocked(apiPost)).toHaveBeenCalledWith('/api/agents/ada/skills/reporter/promote', {
    case_id: 'inv',
    gate_type: 'exact_match',
    ideal_output: 'Acme $42',
  })
})

it('shows the server refusal when a control is refused', async () => {
  const { ApiError } = await import('@/lib/api')
  vi.mocked(apiPost).mockRejectedValue(new ApiError(503, 'agent is not running in this process'))
  renderPanel()
  await userEvent.click(await screen.findByRole('button', { name: /run suite/i }))
  await waitFor(() => expect(screen.getByText('agent is not running in this process')).toBeTruthy())
})

it('a viewer sees the cases but no controls', async () => {
  operator.on = false
  renderPanel()
  await screen.findByText('evals/test_g.py::test_a')
  expect(screen.queryByRole('button', { name: /run suite/i })).toBeNull()
  expect(screen.queryByRole('button', { name: /promote to golden/i })).toBeNull()
})
