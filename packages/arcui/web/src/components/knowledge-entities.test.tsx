// Entities table: TYPE is the one canonical kind; TAGS are topical chips and the
// column disappears when no entity carries a tag (it used to repeat the type).
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { EntityBrowser } from '@/components/knowledge-entities'

const BASE = {
  classification: 'unclassified',
  confidence: 0.9,
  importance: 5,
  source: 'memory/entities/x.md',
  links_to: [],
  facts: [],
  aliases: [],
}

function stub(items: unknown[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL) => {
      const path = String(request)
      const body = path.includes('/knowledge/entities') && !path.includes('/links') ? { items } : { items: [] }
      return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }),
  )
}

function wrap(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TooltipProvider>{node}</TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('Entities table', () => {
  it('hides the Tags column when no entity has a tag', async () => {
    stub([{ ...BASE, slug: 'acme', name: 'Acme', entity_type: 'company', tags: [] }])
    wrap(<EntityBrowser agentId="olivia" selectedSlug={null} onSelectSlug={() => {}} />)
    await screen.findByText('Acme')
    expect(screen.queryByRole('columnheader', { name: 'Tags' })).toBeNull()
    expect(screen.getByRole('columnheader', { name: 'Type' })).toBeTruthy()
  })

  it('shows topical tags as chips', async () => {
    stub([
      { ...BASE, slug: 'acme', name: 'Acme', entity_type: 'company', tags: ['doe', 'federal-sales'] },
      { ...BASE, slug: 'bob', name: 'Bob', entity_type: 'person', tags: [] },
    ])
    wrap(<EntityBrowser agentId="olivia" selectedSlug={null} onSelectSlug={() => {}} />)
    const row = (await screen.findByText('Acme')).closest('tr') as HTMLElement
    expect(screen.getByRole('columnheader', { name: 'Tags' })).toBeTruthy()
    const chips = within(row).getAllByTestId('entity-tag')
    expect(chips.map((c) => c.textContent)).toEqual(['doe', 'federal-sales'])
  })

  it('lists the names a merged entity was also known as', async () => {
    stub([
      {
        ...BASE,
        slug: 'thesis-5',
        name: 'Thesis 5: Multi-Layer Tuning',
        entity_type: 'thesis',
        tags: [],
        aliases: ['Thesis 5', 'thesis-5-bare'],
      },
    ])
    wrap(<EntityBrowser agentId="olivia" selectedSlug="thesis-5" onSelectSlug={() => {}} />)
    expect(await screen.findByText('Also known as')).toBeTruthy()
    expect(screen.getByText('Thesis 5')).toBeTruthy()
  })
})
