// UJ-9 — the one "needs you" inbox. A pending pulse check shows with its agent,
// what it is, why, and an inline Approve that posts to the pulse subsystem's own
// route (no second approval path); approving clears the row.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
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

function stubServer() {
  const posts: { path: string; body: unknown }[] = []
  let pulse = [pulseItem]
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
          schedules: {
            count: 1,
            items: [{ agent_id: 'josh_agent', agent_label: 'Josh Agent', schedule_id: 's1', name: 'weekly' }],
          },
          total: pulse.length + 1,
        })
      }
      if (path === '/api/connections') return json({ connections: [], extensions_roots: [] })
      return json({})
    }),
  )
  return posts
}

function Count() {
  return <span data-testid="count">{useNeedsYouCount()}</span>
}

function renderInbox() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Count />
        <NeedsYouInbox operatorMode />
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
})
