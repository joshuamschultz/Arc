// P16-B — operator "Share to fleet" on the agent's insights/procedures/entities
// plus the Decisions history. Honest errors: a secret has no override; a
// removed card can never be re-shared.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { InsightBrowser } from '@/components/knowledge-insights'
import { ProcedureBrowser } from '@/components/knowledge-procedures'
import { EntityBrowser } from '@/components/knowledge-entities'

const INSIGHT = { id: 'ins-1', statement: 'Retry things', trigger: '', cues: [], instances: [], confidence: 0.9, classification: 'unclassified' }
const PROC = { slug: 'deploy', title: 'Deploy', when_to_use: '', steps: [], use_count: 2, revisions: 1, classification: 'unclassified' }
const ENTITY = { slug: 'acme', name: 'Acme', entity_type: 'org', classification: 'unclassified', confidence: 0.9, importance: 5, source: 'x', links_to: [], facts: [], tags: [] }

type Call = { path: string; method: string; body: unknown }
function stub(share: { status: number; body: unknown } = { status: 200, body: { status: 'published', shared_ref: 'r' } }) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const method = init?.method ?? 'GET'
      calls.push({ path, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      const json = (s: number, b: unknown) =>
        new Response(JSON.stringify(b), { status: s, headers: { 'Content-Type': 'application/json' } })
      if (path.endsWith('/share')) return json(share.status, share.body)
      if (path.endsWith('/decisions')) {
        return json(200, {
          decisions: [
            { decision: 'keep_private', evaluated_at: '2026-10-01T10:00:00Z', label: 'personal', confidence: 0.9, classifier_version: 'jev-1.13.0', decided_by: null, reason: null },
            { decision: 'promoted_by_operator', evaluated_at: '2026-10-02T10:00:00Z', label: null, confidence: null, classifier_version: null, decided_by: 'did:arc:operator', reason: null },
          ],
        })
      }
      if (path.includes('/knowledge/insights')) return json(200, { items: [INSIGHT] })
      if (path.includes('/knowledge/procedures')) return json(200, { items: [PROC] })
      if (path.includes('/knowledge/entities') && !path.includes('/links')) return json(200, { items: [ENTITY] })
      return json(200, { items: [] })
    }),
  )
  return calls
}

function wrap(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter><TooltipProvider>{node}</TooltipProvider></MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => localStorage.setItem('arcui_operator_mode', '1'))
afterEach(() => {
  cleanup()
  localStorage.removeItem('arcui_operator_mode')
  vi.unstubAllGlobals()
})

describe('Share to fleet', () => {
  it('is hidden without operator mode', async () => {
    localStorage.setItem('arcui_operator_mode', '0')
    stub()
    wrap(<InsightBrowser agentId="olivia" />)
    await screen.findByText('Retry things')
    expect(screen.queryByRole('button', { name: /share to fleet/i })).toBeNull()
  })

  it('posts the insight share and confirms', async () => {
    const calls = stub()
    wrap(<InsightBrowser agentId="olivia" />)
    await userEvent.click(await screen.findByRole('button', { name: /share to fleet/i }))
    await waitFor(() => {
      const post = calls.find((c) => c.method === 'POST')
      expect(post?.path).toBe('/api/agents/olivia/knowledge/insight/ins-1/share')
      expect(post?.body).toEqual({})
    })
    expect(await screen.findByText(/shared to the fleet/i)).toBeTruthy()
  })

  it('posts the procedure share with the singular kind', async () => {
    const calls = stub()
    wrap(<ProcedureBrowser agentId="olivia" />)
    await userEvent.click(await screen.findByRole('button', { name: /share to fleet/i }))
    await waitFor(() =>
      expect(calls.find((c) => c.method === 'POST')?.path).toBe('/api/agents/olivia/knowledge/procedure/deploy/share'),
    )
  })

  it('posts the entity share from the entity detail', async () => {
    const calls = stub()
    wrap(<EntityBrowser agentId="olivia" selectedSlug="acme" onSelectSlug={() => {}} />)
    await userEvent.click(await screen.findByRole('button', { name: /share to fleet/i }))
    await waitFor(() =>
      expect(calls.find((c) => c.method === 'POST')?.path).toBe('/api/agents/olivia/knowledge/entity/acme/share'),
    )
  })

  it('says a secret is blocked and cannot be overridden', async () => {
    stub({ status: 422, body: { status: 'blocked_secret', shared_ref: null } })
    wrap(<InsightBrowser agentId="olivia" />)
    await userEvent.click(await screen.findByRole('button', { name: /share to fleet/i }))
    expect(await screen.findByText(/no override/i)).toBeTruthy()
    expect(screen.getByText(/secret/i)).toBeTruthy()
  })

  it('says a removed card can never be shared again', async () => {
    stub({ status: 409, body: { status: 'demoted', shared_ref: null } })
    wrap(<InsightBrowser agentId="olivia" />)
    await userEvent.click(await screen.findByRole('button', { name: /share to fleet/i }))
    expect(await screen.findByText(/removed this card from the fleet/i)).toBeTruthy()
    expect(screen.getByText(/never be shared again/i)).toBeTruthy()
  })

  it('says an unknown outcome was not confirmed', async () => {
    stub({ status: 202, body: { status: 'outcome_unknown', shared_ref: null } })
    wrap(<InsightBrowser agentId="olivia" />)
    await userEvent.click(await screen.findByRole('button', { name: /share to fleet/i }))
    expect(await screen.findByText(/could not confirm/i)).toBeTruthy()
  })

  it('explains a viewer or tier refusal', async () => {
    stub({ status: 403, body: { status: 'tier_forbidden', shared_ref: null } })
    wrap(<InsightBrowser agentId="olivia" />)
    await userEvent.click(await screen.findByRole('button', { name: /share to fleet/i }))
    expect(await screen.findByText(/not allowed/i)).toBeTruthy()
  })
})

describe('Decisions history', () => {
  it('lists classifier and operator decisions for the card', async () => {
    const calls = stub()
    wrap(<InsightBrowser agentId="olivia" />)
    await userEvent.click(await screen.findByRole('button', { name: /decisions/i }))
    expect(await screen.findByText(/kept private/i)).toBeTruthy()
    expect(screen.getByText(/shared by operator/i)).toBeTruthy()
    expect(calls.some((c) => c.path === '/api/agents/olivia/knowledge/insight/ins-1/decisions')).toBe(true)
  })

  it('is visible to viewers too', async () => {
    localStorage.setItem('arcui_operator_mode', '0')
    stub()
    wrap(<InsightBrowser agentId="olivia" />)
    expect(await screen.findByRole('button', { name: /decisions/i })).toBeTruthy()
  })
})
