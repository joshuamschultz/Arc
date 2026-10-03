// UJ-9 — the one "needs you" inbox. A pending pulse check shows with its agent,
// what it is, why, and an inline Approve that posts to the pulse subsystem's own
// route (no second approval path); approving clears the row.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { ApprovalsPage } from '@/pages/approvals'
import { NeedsYouInbox } from '@/components/needs-you-inbox'
import { useNeedsYouCount } from '@/hooks/use-needs-you-count'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const empty = { count: 0, items: [] }
const pulseItem = {
  agent_id: 'josh_agent',
  agent_label: 'Josh Agent',
  check: 'daily_briefing',
  interval_minutes: 60,
  action: 'Brief the operator',
  definition_digest: 'd1',
  changed: false,
}

const LONG_ACTION =
  'Prepare my daily briefing. Check my Slack channels and my inbox and my calendar for anything urgent, then summarise it in five bullets.'
const connection = (instance: string, agents: string[]) => ({
  instance,
  extension: instance,
  extension_display_name: instance === 'confluence' ? 'Confluence' : instance,
  agents,
  display_status: 'needs_you',
  reason_text: 'Sign in again',
  action_label: 'Reconnect',
})
const roster = [
  { agent_id: 'josh_agent', display_name: 'Olivia', workspace_path: '/x/josh_agent' },
  { agent_id: 'simple_olivia', display_name: 'Deep Olivia', workspace_path: '/x/simple_olivia' },
  { agent_id: 'marketer_agent', display_name: 'MC', workspace_path: '/x/marketer_agent' },
  { agent_id: 'fourth', display_name: 'Fourth', workspace_path: '/x/fourth' },
]

interface Stub {
  pulse?: (typeof pulseItem)[]
  schedules?: unknown[]
  connections?: unknown[]
  approvals?: unknown[]
}

function stubServer(stub: Stub = {}) {
  const posts: { path: string; body: unknown }[] = []
  let pulse = stub.pulse ?? [pulseItem]
  const schedules = stub.schedules ?? [
    { agent_id: 'josh_agent', agent_label: 'Josh Agent', schedule_id: 's1', name: 'weekly' },
  ]
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
      if (init?.method === 'POST') {
        posts.push({ path, body: JSON.parse(String(init.body)) })
        pulse = []
        return json({ approved: true })
      }
      if (path === '/api/home/needs') {
        return json({
          approvals: empty,
          capabilities: empty,
          review_tasks: empty,
          waiting_on_human: empty,
          pulse: { count: pulse.length, items: pulse },
          schedules: { count: schedules.length, items: schedules },
          total: pulse.length + schedules.length,
        })
      }
      if (path === '/api/connections')
        return json({ connections: stub.connections ?? [], extensions_roots: [] })
      if (path === '/api/team/roster') return json({ agents: roster })
      if (path === '/api/approvals') return json({ approvals: stub.approvals ?? [] })
      return json({})
    }),
  )
  return posts
}

function Count() {
  return <span data-testid="count">{useNeedsYouCount()}</span>
}

function renderInbox(page = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Count />
        {page ? <ApprovalsPage /> : <NeedsYouInbox operatorMode />}
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('NeedsYouInbox', () => {
  it('shows a pending pulse check and clears it once approved through the pulse route', async () => {
    const posts = stubServer()
    renderInbox()

    const row = await screen.findByTestId('needs-pulse-josh_agent-daily_briefing')
    expect(row.textContent).toContain('Josh Agent')
    expect(row.textContent).toContain('Brief the operator')
    expect(row.textContent).toContain('Never approved')
    expect(screen.getByTestId('count').textContent).toBe('2')

    await userEvent.click(screen.getByRole('button', { name: 'Approve daily_briefing' }))

    await waitFor(() => expect(screen.queryByTestId('needs-pulse-josh_agent-daily_briefing')).toBeNull())
    expect(posts).toEqual([
      {
        path: '/api/agents/josh_agent/pulse/approve',
        body: { check: 'daily_briefing', definition_digest: 'd1' },
      },
    ])
  })

  it('shows an unapproved schedule with an inline approve', async () => {
    stubServer()
    renderInbox()
    expect(await screen.findByTestId('needs-schedule-josh_agent-s1')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Approve schedule weekly' })).toBeTruthy()
  })

  it('wraps a long pulse action inside the card, folds it, and keeps Approve visible', async () => {
    stubServer({ pulse: [{ ...pulseItem, interval_minutes: 1440, action: LONG_ACTION }], schedules: [] })
    renderInbox()
    const row = await screen.findByTestId('needs-pulse-josh_agent-daily_briefing')

    const diff = screen.getByTestId('diff-block')
    for (const cls of ['whitespace-pre-wrap', 'break-words', 'max-w-full']) {
      expect(diff.className).toContain(cls)
    }
    expect(diff.className).not.toContain('overflow-x-auto')
    expect(diff.className).toContain('max-h-24')
    // 375px and 1440px: the row stacks on phones, runs side by side from sm up,
    // and the text column may shrink instead of pushing the page wide.
    expect(row.className).toContain('flex-col')
    expect(row.className).toContain('sm:flex-row')
    expect(row.className).toContain('min-w-0')
    expect(row.querySelector('.min-w-0.flex-1')).toBeTruthy()

    await userEvent.click(screen.getByRole('button', { name: 'Show all' }))
    expect(screen.getByTestId('diff-block').className).not.toContain('max-h-24')
    expect(screen.getByRole('button', { name: 'Show less' })).toBeTruthy()

    expect(screen.getByRole('button', { name: 'Approve daily_briefing' })).toBeTruthy()
  })

  it('words the cadence once, not "every Every"', async () => {
    stubServer({ pulse: [{ ...pulseItem, interval_minutes: 1440 }], schedules: [] })
    renderInbox()
    const row = await screen.findByTestId('needs-pulse-josh_agent-daily_briefing')
    expect(row.textContent).toContain('Pulse check daily_briefing · every 1 day')
    expect(row.textContent).not.toMatch(/every\s+every/i)
  })

  it('shows display names for connection agents, caps at three, and names the connection in plain text', async () => {
    stubServer({
      pulse: [],
      schedules: [],
      connections: [connection('confluence', ['josh_agent', 'simple_olivia', 'marketer_agent', 'fourth'])],
    })
    renderInbox()
    const row = await screen.findByTestId('needs-connection-confluence')
    expect(row.textContent).toContain('Olivia, Deep Olivia, MC +1 more')
    expect(row.textContent).not.toContain('josh_agent')
    expect(row.textContent).toContain('Connection Confluence needs you')
    expect(row.querySelector('.font-mono')).toBeNull()
  })

  it('shows no empty panel while other items wait, and one when everything is empty', async () => {
    stubServer({ pulse: [], schedules: [], connections: [connection('confluence', ['josh_agent'])] })
    renderInbox(true)
    await screen.findByTestId('needs-connection-confluence')
    await waitFor(() => expect(screen.queryByText('Nothing here yet')).toBeNull())
    cleanup()

    stubServer({ pulse: [], schedules: [] })
    renderInbox(true)
    expect(await screen.findAllByText('Nothing here yet')).toHaveLength(1)
  })
})
