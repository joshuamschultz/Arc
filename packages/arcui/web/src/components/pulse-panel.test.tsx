// P47 — the "Pulse approvals" panel on the agent page.
//
//   <PulsePanel agentId="olivia" operatorMode={true|false} />
//   GET  /api/agents/{id}/pulse
//        -> { checks: [{ name, interval_minutes, action, definition_digest, status,
//             approved, stale, approved_revision, last_revision_ran, diff }],
//             authority_available }
//   POST /api/agents/{id}/pulse/approve  { check, definition_digest }
//
// The operator reviews each unapproved check with its diff and approves the exact
// definition shown (the request carries the digest the list returned).
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
const APPROVE_PATH = '/api/agents/olivia/pulse/approve'

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

const check = (over: Partial<Check> & { name: string }): Check => ({
  interval_minutes: 5,
  action: 'Sweep the inbox',
  definition_digest: `digest-${over.name}`,
  status: 'unapproved',
  approved: false,
  stale: false,
  approved_revision: null,
  last_revision_ran: null,
  diff: '+action: Sweep the inbox',
  ...over,
})

type Call = { path: string; method: string; body: unknown }

function stubServer({
  checks,
  authority = true,
  approveStatus = 200,
}: {
  checks: Check[]
  authority?: boolean
  approveStatus?: number
}) {
  const calls: Call[] = []
  let current = checks
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const method = init?.method ?? 'GET'
      const raw = init?.body ? String(init.body) : ''
      calls.push({ path, method, body: raw ? JSON.parse(raw) : undefined })
      const json = (status: number, body: unknown) =>
        new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
      if (path === APPROVE_PATH && method === 'POST') {
        if (approveStatus !== 200) return json(approveStatus, { error: 'pulse check changed since it was reviewed' })
        const { check: name } = JSON.parse(raw) as { check: string }
        current = current.map((c) =>
          c.name === name ? { ...c, status: 'approved', approved: true, stale: false, approved_revision: 1, diff: '' } : c,
        )
        return json(200, { check: name, revision: 1, approved: true })
      }
      if (path === LIST_PATH && method === 'GET') return json(200, { checks: current, authority_available: authority })
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

describe('PulsePanel', () => {
  it('lists each check with its approval state', async () => {
    stubServer({
      checks: [
        check({ name: 'health' }),
        check({ name: 'inbox', status: 'changes_pending', stale: true, approved_revision: 1 }),
        check({ name: 'disk', status: 'approved', approved: true, approved_revision: 2, last_revision_ran: 2, diff: '' }),
      ],
    })
    renderPanel()

    await screen.findByRole('heading', { name: /pulse checks/i })
    expect(await screen.findByText('health')).toBeTruthy()
    expect(screen.getByText(/^pending approval$/i)).toBeTruthy()
    expect(screen.getByText(/changes pending approval/i)).toBeTruthy()
    expect(screen.getByText(/last ran revision 2/i)).toBeTruthy()
  })

  it('shows the diff against the last approved revision', async () => {
    stubServer({
      checks: [
        check({
          name: 'inbox',
          status: 'changes_pending',
          stale: true,
          approved_revision: 1,
          diff: '-action: Old\n+action: Exfiltrate',
        }),
      ],
    })
    renderPanel()

    expect(await screen.findByText(/\+action: Exfiltrate/)).toBeTruthy()
    expect(screen.getByText(/-action: Old/)).toBeTruthy()
  })

  it('approves exactly the definition it showed, then refreshes', async () => {
    const calls = stubServer({ checks: [check({ name: 'health' })] })
    renderPanel()

    await userEvent.click(await screen.findByRole('button', { name: /approve health/i }))

    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
    const posts = calls.filter((c) => c.method === 'POST')
    expect(posts).toHaveLength(1)
    expect(posts[0].path).toBe(APPROVE_PATH)
    expect(posts[0].body).toEqual({ check: 'health', definition_digest: 'digest-health' })
    await waitFor(() => expect(screen.queryByRole('button', { name: /approve health/i })).toBeNull())
    expect(screen.getByText(/^approved$/i)).toBeTruthy()
  })

  it('does not offer approval for an already approved check', async () => {
    stubServer({ checks: [check({ name: 'disk', status: 'approved', approved: true, diff: '' })] })
    renderPanel()

    await screen.findByText('disk')
    expect(screen.queryByRole('button', { name: /approve disk/i })).toBeNull()
  })

  it('is read-only outside operator mode', async () => {
    const calls = stubServer({ checks: [check({ name: 'health' })] })
    renderPanel(false)

    await screen.findByText('health')
    expect(screen.queryByRole('button', { name: /approve/i })).toBeNull()
    expect(screen.getByText(/operator mode/i)).toBeTruthy()
    expect(calls.filter((c) => c.method === 'POST')).toHaveLength(0)
  })

  it('shows the server refusal and keeps the check unapproved', async () => {
    stubServer({ checks: [check({ name: 'health' })], approveStatus: 409 })
    renderPanel()

    await userEvent.click(await screen.findByRole('button', { name: /approve health/i }))

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText(/changed since it was reviewed/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /approve health/i })).toBeTruthy()
  })

  it('says so when the control authority is unavailable', async () => {
    stubServer({ checks: [check({ name: 'health' })], authority: false })
    renderPanel()

    expect(await screen.findByText(/approval is unavailable/i)).toBeTruthy()
    expect((screen.getByRole('button', { name: /approve health/i }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('has an empty state', async () => {
    stubServer({ checks: [] })
    renderPanel()

    expect(await screen.findByText(/no pulse checks/i)).toBeTruthy()
  })
})
