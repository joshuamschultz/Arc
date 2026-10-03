// Pulse add / edit / remove from the agent page.
//
//   POST   /api/agents/{id}/pulse                    { name, interval_minutes, action, proposal? }
//   PUT    /api/agents/{id}/pulse/{name}             { interval_minutes, action, definition_digest }
//   DELETE /api/agents/{id}/pulse/{name}             { definition_digest }
//   DELETE /api/agents/{id}/pulse/proposals/{name}
//   GET    /api/agents/{id}/pulse -> { checks, proposals, authority_available }
//
// A write never approves: the new or edited check reads "pending approval" and goes
// through the existing Approve button. Viewers see no write controls.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { PulsePanel } from '@/components/pulse-panel'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const LIST_PATH = '/api/agents/olivia/pulse'

type Check = {
  name: string
  interval_minutes: number
  action: string
  definition_digest: string
  status: 'approved' | 'unapproved' | 'changes_pending'
  approved: boolean
  stale: boolean
  approved_revision: number | null
  last_revision_ran: number | null
  diff: string
}
type Proposal = { name: string; interval_minutes: number; action: string; reason: string }
type Call = { path: string; method: string; body: unknown }

const check = (name: string, over: Partial<Check> = {}): Check => ({
  name,
  interval_minutes: 60,
  action: 'Sweep the inbox',
  definition_digest: `digest-${name}`,
  status: 'approved',
  approved: true,
  stale: false,
  approved_revision: 1,
  last_revision_ran: 1,
  diff: '',
  ...over,
})

function stubServer({
  checks = [],
  proposals = [],
  writeStatus = 200,
}: {
  checks?: Check[]
  proposals?: Proposal[]
  writeStatus?: number
}) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const method = init?.method ?? 'GET'
      const raw = init?.body ? String(init.body) : ''
      calls.push({ path, method, body: raw ? JSON.parse(raw) : undefined })
      const json = (status: number, body: unknown) =>
        new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
      if (path === LIST_PATH && method === 'GET') {
        return json(200, { checks, proposals, authority_available: true })
      }
      if (method !== 'GET') {
        return writeStatus === 200 ? json(200, { ok: true }) : json(writeStatus, { error: 'refused by server' })
      }
      return json(404, { error: 'not found' })
    }),
  )
  return calls
}

function renderPanel(operatorMode = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <PulsePanel agentId="olivia" operatorMode={operatorMode} />
    </QueryClientProvider>,
  )
}

const writes = (calls: Call[]) => calls.filter((c) => c.method !== 'GET')

describe('PulsePanel empty state', () => {
  it('explains what pulse is, shows an example, and offers Add check', async () => {
    stubServer({})
    renderPanel()

    expect(await screen.findByText(/periodic checks/i)).toBeTruthy()
    expect(screen.getByText(/for example/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /add check/i })).toBeTruthy()
  })

  it('hides Add check from viewers but still explains pulse', async () => {
    stubServer({})
    renderPanel(false)

    expect(await screen.findByText(/periodic checks/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /add check/i })).toBeNull()
  })
})

describe('Add check', () => {
  async function openForm() {
    await userEvent.click(await screen.findByRole('button', { name: /add check/i }))
  }

  it('posts the new check as minutes and shows a human preview of the interval', async () => {
    const calls = stubServer({})
    renderPanel()
    await openForm()

    await userEvent.type(screen.getByLabelText(/^name/i), 'inbox')
    await userEvent.type(screen.getByLabelText(/what to check/i), 'Sweep the inbox for urgent mail')
    await userEvent.clear(screen.getByLabelText(/every/i))
    await userEvent.type(screen.getByLabelText(/every/i), '2')
    await userEvent.selectOptions(screen.getByLabelText(/unit/i), 'hours')
    expect(screen.getByText(/runs every 2 hours/i)).toBeTruthy()

    await userEvent.click(screen.getByRole('button', { name: /save check/i }))

    await waitFor(() => expect(writes(calls)).toHaveLength(1))
    expect(writes(calls)[0]).toEqual({
      path: LIST_PATH,
      method: 'POST',
      body: { name: 'inbox', interval_minutes: 120, action: 'Sweep the inbox for urgent mail' },
    })
  })

  it('refuses an invalid name before calling the server', async () => {
    const calls = stubServer({})
    renderPanel()
    await openForm()

    await userEvent.type(screen.getByLabelText(/^name/i), 'bad name')
    await userEvent.type(screen.getByLabelText(/what to check/i), 'x')
    await userEvent.click(screen.getByRole('button', { name: /save check/i }))

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(writes(calls)).toHaveLength(0)
  })

  it('shows the server refusal', async () => {
    stubServer({ writeStatus: 409 })
    renderPanel()
    await openForm()

    await userEvent.type(screen.getByLabelText(/^name/i), 'inbox')
    await userEvent.type(screen.getByLabelText(/what to check/i), 'x')
    await userEvent.click(screen.getByRole('button', { name: /save check/i }))

    expect(await screen.findByText(/refused by server/i)).toBeTruthy()
  })

  it('says the new check needs approval after saving', async () => {
    stubServer({})
    renderPanel()
    await openForm()
    await userEvent.type(screen.getByLabelText(/^name/i), 'inbox')
    await userEvent.type(screen.getByLabelText(/what to check/i), 'x')
    await userEvent.click(screen.getByRole('button', { name: /save check/i }))

    expect(await screen.findByText(/saved\. approve it below/i)).toBeTruthy()
  })
})

describe('Edit and remove', () => {
  it('edits with the digest it showed', async () => {
    const calls = stubServer({ checks: [check('health')] })
    renderPanel()

    await userEvent.click(await screen.findByRole('button', { name: /edit health/i }))
    const action = screen.getByLabelText(/what to check/i)
    await userEvent.clear(action)
    await userEvent.type(action, 'Deeper check')
    await userEvent.click(screen.getByRole('button', { name: /save check/i }))

    await waitFor(() => expect(writes(calls)).toHaveLength(1))
    expect(writes(calls)[0]).toEqual({
      path: `${LIST_PATH}/health`,
      method: 'PUT',
      body: { interval_minutes: 60, action: 'Deeper check', definition_digest: 'digest-health' },
    })
  })

  it('removes only after confirmation, with the digest', async () => {
    const calls = stubServer({ checks: [check('health')] })
    renderPanel()

    await userEvent.click(await screen.findByRole('button', { name: /delete health/i }))
    expect(writes(calls)).toHaveLength(0)
    await userEvent.click(screen.getByRole('button', { name: /confirm delete health/i }))

    await waitFor(() => expect(writes(calls)).toHaveLength(1))
    expect(writes(calls)[0]).toEqual({
      path: `${LIST_PATH}/health`,
      method: 'DELETE',
      body: { definition_digest: 'digest-health' },
    })
  })

  it('offers no write controls to viewers', async () => {
    stubServer({ checks: [check('health')] })
    renderPanel(false)

    await screen.findByText('health')
    expect(screen.queryByRole('button', { name: /edit health/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /delete health/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /add check/i })).toBeNull()
  })
})

describe('Agent proposals', () => {
  const proposal: Proposal = {
    name: 'queue',
    interval_minutes: 30,
    action: 'Watch the queue',
    reason: 'It backs up on Mondays',
  }

  it('lists a proposal with the agent reason and opens it pre-filled', async () => {
    stubServer({ proposals: [proposal] })
    renderPanel()

    expect(await screen.findByText(/proposed by the agent/i)).toBeTruthy()
    expect(screen.getByText(/backs up on mondays/i)).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: /review queue/i }))
    expect((screen.getByLabelText(/^name/i) as HTMLInputElement).value).toBe('queue')
    expect((screen.getByLabelText(/what to check/i) as HTMLTextAreaElement).value).toBe('Watch the queue')
  })

  it('accepting a proposal posts it with its name so the server clears it', async () => {
    const calls = stubServer({ proposals: [proposal] })
    renderPanel()

    await userEvent.click(await screen.findByRole('button', { name: /review queue/i }))
    await userEvent.click(screen.getByRole('button', { name: /save check/i }))

    await waitFor(() => expect(writes(calls)).toHaveLength(1))
    expect(writes(calls)[0].body).toMatchObject({ name: 'queue', proposal: 'queue' })
  })

  it('dismisses a proposal', async () => {
    const calls = stubServer({ proposals: [proposal] })
    renderPanel()

    await userEvent.click(await screen.findByRole('button', { name: /dismiss queue/i }))

    await waitFor(() => expect(writes(calls)).toHaveLength(1))
    expect(writes(calls)[0]).toMatchObject({ path: `${LIST_PATH}/proposals/queue`, method: 'DELETE' })
  })
})
