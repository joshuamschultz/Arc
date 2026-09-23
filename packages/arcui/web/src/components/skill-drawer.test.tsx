import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SkillDrawer } from '@/components/skill-drawer'
import { apiGet, apiPut, ApiError } from '@/lib/api'

vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  apiGet: vi.fn(),
  apiPut: vi.fn(),
}))
vi.mock('@/hooks/use-operator-mode', () => ({ useOperatorMode: () => [true, vi.fn()] }))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('keeps a stale-digest edit open and never retries it against a new hash', async () => {
  vi.mocked(apiGet).mockImplementation(async (path) => path.endsWith('/versions')
    ? { items: [] }
    : {
      name: 'reporter', version: '1', source_root: 'agent-skills', source_path: 'SKILL.md',
      status: 'loaded', status_detail: '', content: 'old body', sha256: 'old-digest',
      editable: true, write_root: 'agent', write_path: 'capabilities/skills/reporter/SKILL.md',
    })
  vi.mocked(apiPut).mockRejectedValue(new ApiError(409, 'skill changed since it was opened'))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}>
    <SkillDrawer agentId="ada" skillName="reporter" open onOpenChange={() => {}} />
  </QueryClientProvider>)
  await screen.findByRole('button', { name: 'Edit' })
  await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
  const draft = screen.getByRole('textbox') as HTMLTextAreaElement
  await userEvent.clear(draft)
  await userEvent.type(draft, 'my reviewed revision')
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(screen.getByText('skill changed since it was opened')).toBeTruthy())
  expect(draft.value).toBe('my reviewed revision')
  expect(vi.mocked(apiPut)).toHaveBeenCalledWith(
    '/api/agents/ada/skills/reporter/revision',
    { content: 'my reviewed revision', expected_sha256: 'old-digest' },
  )
  expect(vi.mocked(apiPut)).toHaveBeenCalledTimes(1)
  client.clear()
})
