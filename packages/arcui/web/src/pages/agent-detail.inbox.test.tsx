// Item 2 — the agent Inbox tab shows mail, not delivery channels.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { InboxTab } from '@/pages/agent-detail'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function stubFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL) => {
      const path = String(request)
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      if (path.includes('/channels')) {
        return json({ channels: [{ target: 'web:abc', label: 'Web session abc' }] })
      }
      if (path.includes('/inbox')) return json({ threads: [] })
      if (path.includes('/tasks')) return json({ tasks: [] })
      if (path.includes('/approvals')) return json({ approvals: [] })
      return json({ agents: [] })
    }),
  )
}

function renderInbox() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <InboxTab agentId="olivia" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('InboxTab', () => {
  it('has no delivery channels and shows the three mail stat cards', async () => {
    stubFetch()
    renderInbox()

    expect(await screen.findByText('Pending approvals')).toBeTruthy()
    expect(screen.getByText('Awaiting review')).toBeTruthy()
    expect(screen.getAllByText('Inbox threads').length).toBeGreaterThan(0)
    expect(screen.queryByText(/delivery channels/i)).toBeNull()
    expect(screen.queryByText('Web session abc')).toBeNull()
  })
})
