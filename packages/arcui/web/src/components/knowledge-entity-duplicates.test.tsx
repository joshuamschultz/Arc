// "Review duplicates" on the Entities view: proposed merges with Merge / Not the same.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { EntityDuplicatesPanel } from '@/components/knowledge-entity-duplicates'

const PROPOSAL = {
  slugs: ['harness-advantage', 'harness-advantage-thesis'],
  survivor: 'harness-advantage',
  name: 'Harness Advantage',
  entity_type: 'thesis',
  classification: 'unclassified',
  basis: 'candidate',
  cards: [
    { slug: 'harness-advantage', name: 'Harness Advantage', entity_type: 'document', classification: 'unclassified', tags: ['arc'], facts: 2 },
    { slug: 'harness-advantage-thesis', name: 'Harness Advantage', entity_type: 'thesis', classification: 'unclassified', tags: [], facts: 1 },
  ],
}

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

/** A tiny server: proposals until an action is posted, then none. */
function stub(actionStatus = 200) {
  const posts: Array<{ path: string; body: unknown }> = []
  let open = true
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      if (init?.method === 'POST') {
        posts.push({ path, body: JSON.parse(String(init.body)) })
        if (actionStatus !== 200) return json(actionStatus, { error: 'refused' })
        open = false
        return json(200, { status: 'applied', survivor: 'harness-advantage' })
      }
      if (path.endsWith('/knowledge/entities/duplicates')) {
        return json(200, { items: open ? [PROPOSAL] : [] })
      }
      return json(200, { items: [] })
    }),
  )
  return posts
}

function wrap() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <EntityDuplicatesPanel agentId="olivia" />
    </QueryClientProvider>,
  )
}

beforeEach(() => localStorage.setItem('arcui_operator_mode', '1'))
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.removeItem('arcui_operator_mode')
})

describe('Review duplicates', () => {
  it('lists each proposal with the type the merged card keeps', async () => {
    stub()
    wrap()
    fireEvent.click(await screen.findByRole('button', { name: /Review duplicates \(1\)/ }))
    expect(screen.getAllByTestId('duplicate-proposal')).toHaveLength(1)
    expect(screen.getByTestId('proposal-type').textContent).toBe('thesis')
    expect(screen.getByText(/document · unclassified · 2 facts/)).toBeTruthy()
  })

  it('Merge posts the proposal and the panel empties', async () => {
    const posts = stub()
    wrap()
    fireEvent.click(await screen.findByRole('button', { name: /Review duplicates/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Merge' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: /Review duplicates/ })).toBeNull())
    expect(posts).toEqual([
      {
        path: '/api/agents/olivia/knowledge/entities/duplicates/merge',
        body: { slugs: ['harness-advantage', 'harness-advantage-thesis'] },
      },
    ])
  })

  it('Not the same posts a rejection', async () => {
    const posts = stub()
    wrap()
    fireEvent.click(await screen.findByRole('button', { name: /Review duplicates/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Not the same' }))
    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0].path).toBe('/api/agents/olivia/knowledge/entities/duplicates/reject')
  })

  it('says why a refused merge did not happen', async () => {
    stub(409)
    wrap()
    fireEvent.click(await screen.findByRole('button', { name: /Review duplicates/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Merge' }))
    expect((await screen.findByRole('alert')).textContent).toMatch(/cannot be one thing/)
  })

  it('hides the buttons without operator mode', async () => {
    localStorage.removeItem('arcui_operator_mode')
    stub()
    wrap()
    fireEvent.click(await screen.findByRole('button', { name: /Review duplicates/ }))
    expect(screen.queryByRole('button', { name: 'Merge' })).toBeNull()
  })

  it('renders nothing when there is nothing to review', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json(200, { items: [] })))
    wrap()
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByRole('button', { name: /Review duplicates/ })).toBeNull()
  })
})
