// Profile review left the Knowledge page: facts waiting for approval now show up in
// the Needs-you inbox, and the same approve/decline panel opens from the row.
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
const FACT = {
  fact_id: 'f1',
  profile_id: 'p1',
  field: 'Favourite airline',
  value: 'Delta',
  kind: 'preference',
  status: 'pending',
  classification: 'internal',
  source_id: 'mail-1',
  external_id: 'e1',
  replaces_fact_id: null,
}

function stubServer(initial: unknown[]) {
  const posts: string[] = []
  let pending = initial
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
      if (init?.method === 'POST') {
        posts.push(path)
        pending = []
        return json({ ...FACT, status: 'approved' })
      }
      if (path === '/api/home/needs')
        return json({ approvals: empty, capabilities: empty, review_tasks: empty, waiting_on_human: empty, pulse: empty, schedules: empty, total: 0 })
      if (path === '/api/connections') return json({ connections: [], extensions_roots: [] })
      if (path === '/api/team/roster')
        return json({
          agents: [
            { agent_id: 'josh_agent', display_name: 'Olivia', workspace_path: '/x/josh_agent' },
            { agent_id: 'other', display_name: 'Other', workspace_path: '/x/other' },
          ],
        })
      if (path.startsWith('/api/agents/josh_agent/knowledge/profile-reviews')) return json({ items: pending })
      return json({ items: [] })
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

describe('Needs-you inbox: profile facts waiting for review', () => {
  it('shows a row for an agent with pending facts and counts it in the badge', async () => {
    stubServer([FACT])
    renderInbox()

    const row = await screen.findByTestId('needs-profile-josh_agent')
    expect(row.textContent).toContain('Olivia')
    expect(row.textContent).toContain('1 profile fact waiting for review')
    expect(screen.queryByTestId('needs-profile-other')).toBeNull()
    await waitFor(() => expect(screen.getByTestId('count').textContent).toBe('1'))
  })

  it('opens the review panel from the row and approves a fact through the same route', async () => {
    const posts = stubServer([FACT])
    renderInbox()
    await userEvent.click(await screen.findByRole('button', { name: /review profile facts for olivia/i }))

    expect(await screen.findByRole('heading', { name: 'Profile facts waiting for review' })).toBeTruthy()
    expect(await screen.findByText('Favourite airline')).toBeTruthy()
    expect(screen.getByText('Delta')).toBeTruthy()

    await userEvent.click(screen.getByRole('button', { name: /^approve$/i }))

    await waitFor(() => expect(posts).toEqual(['/api/agents/josh_agent/knowledge/profile-reviews/f1/approve']))
  })

  it('shows no row when nothing is waiting', async () => {
    stubServer([])
    renderInbox()
    await waitFor(() => expect(screen.getByTestId('count').textContent).toBe('0'))
    expect(screen.queryByTestId('needs-profile-josh_agent')).toBeNull()
  })

  it('stacks the row on a phone', async () => {
    stubServer([FACT])
    renderInbox()
    const row = await screen.findByTestId('needs-profile-josh_agent')
    expect(row.className).toContain('flex-col')
    expect(row.className).toContain('sm:flex-row')
    expect(row.className).toContain('min-w-0')
  })
})
