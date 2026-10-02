// Item 10 — the policy document is collapsed until the operator opens it.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { PolicyTab } from '@/pages/agent-detail'

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
      if (path.includes('/policy/stats')) {
        return json({ total: 0, active: 0, retired: 0, avg_score: 0 })
      }
      if (path.includes('/policy')) {
        return json({ bullets: [], raw: 'Zebra rule: never mix concerns' })
      }
      if (path.includes('/config')) return json({ config: {} })
      return json({})
    }),
  )
}

describe('PolicyTab', () => {
  it('collapses the policy document by default and opens on click', async () => {
    stubFetch()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <PolicyTab agentId="olivia" />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    const summary = await screen.findByText('Policy document')
    const details = summary.closest('details')
    expect(details).not.toBeNull()
    expect(details!.open).toBe(false)

    await userEvent.click(summary)
    expect(details!.open).toBe(true)
    expect(screen.getByText(/zebra rule/i)).toBeTruthy()
  })
})
