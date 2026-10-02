// P16-B — Shared knowledge page: typed tabs with counts, honest titles and
// classification badges, owner/contributors/time, provenance drawer, operator
// Remove (demote) with a required reason + permanent confirm, include-removed.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { SharedKnowledgePage } from '@/pages/shared-knowledge'

type Doc = Record<string, unknown>
const doc = (over: Doc = {}): Doc => ({
  identifier: 'ins-1',
  title: 'Retry with backoff',
  kind: 'insight',
  classification: 'unclassified',
  tags: ['ops'],
  owner_did: 'did:arc:olivia',
  owner_display: 'Olivia',
  contributors: [{ did: 'did:arc:max', display: 'Max' }],
  promoted_at: '2026-10-01T12:00:00Z',
  excerpt: 'Always retry network calls with exponential backoff.',
  demotion: null,
  ...over,
})
const DOCS: Doc[] = [
  doc(),
  doc({ identifier: 'proc-1', title: 'Deploy checklist', kind: 'procedure', excerpt: 'Run tests then deploy.' }),
  doc({ identifier: 'ent-1', title: 'Acme Corp', kind: 'entity', classification: 'CUI', excerpt: 'Acme Corp' }),
]
const COUNTS = { all: 3, insight: 1, procedure: 1, entity: 1, demoted: 1 }
const DETAIL = {
  identifier: 'ins-1',
  title: 'Retry with backoff',
  kind: 'insight',
  content: 'FULL BODY: retry network calls with backoff and jitter.',
  classification: 'unclassified',
  tags: ['ops'],
  contributors: [{ did: 'did:arc:max', display: 'Max' }],
  promoted_at: '2026-10-01T12:00:00Z',
  provenance: [
    {
      contributor_did: 'did:arc:olivia',
      contributor_display: 'Olivia',
      source_ref: 'insight:ins-1',
      digest: 'abc123',
      kind: 'insight',
      decision: 'classifier_promote',
      confidence: 0.97,
      classifier_version: 'jev-1.13.0',
      decided_by: null,
      promoted_at: '2026-10-01T12:00:00Z',
    },
    {
      contributor_did: 'did:arc:max',
      contributor_display: 'Max',
      source_ref: 'insight:ins-9',
      digest: 'def456',
      kind: 'insight',
      decision: 'operator_promote',
      confidence: null,
      classifier_version: null,
      decided_by: 'did:arc:operator',
      promoted_at: '2026-10-01T13:00:00Z',
    },
  ],
}

type Call = { path: string; method: string; body: unknown }
function stub(opts: { demoteStatus?: number } = {}) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const method = init?.method ?? 'GET'
      calls.push({ path, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      const json = (s: number, b: unknown) =>
        new Response(JSON.stringify(b), { status: s, headers: { 'Content-Type': 'application/json' } })
      if (path.startsWith('/api/team/knowledge/shared/ins-1/demote') && method === 'POST') {
        if (opts.demoteStatus && opts.demoteStatus !== 200) {
          return json(opts.demoteStatus, { error: 'Operator role required' })
        }
        return json(200, { identifier: 'ins-1', demoted_by: 'did:arc:operator', reason: 'x', demoted_at: '2026-10-02T00:00:00Z' })
      }
      if (path.startsWith('/api/team/knowledge/shared/ins-1')) return json(200, DETAIL)
      if (path.startsWith('/api/team/knowledge/shared')) {
        const url = new URL(path, 'http://x')
        const kind = url.searchParams.get('kind')
        let documents = kind ? DOCS.filter((d) => d.kind === kind) : [...DOCS]
        if (url.searchParams.get('include_demoted') === '1') {
          documents = [
            ...documents,
            doc({
              identifier: 'gone-1',
              title: 'Removed card',
              excerpt: 'Removed body',
              demotion: { demoted_by: 'did:arc:operator', reason: 'was wrong', demoted_at: '2026-10-02T00:00:00Z' },
            }),
          ]
        }
        return json(200, { documents, counts: COUNTS })
      }
      return json(200, { agents: [] })
    }),
  )
  return calls
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TooltipProvider>
          <SharedKnowledgePage />
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => localStorage.removeItem('arcui_operator_mode'))
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('SharedKnowledgePage tabs', () => {
  it('shows typed tabs with counts', async () => {
    stub()
    renderPage()
    expect(await screen.findByRole('tab', { name: /insights\s*1/i })).toBeTruthy()
    expect(screen.getByRole('tab', { name: /procedures\s*1/i })).toBeTruthy()
    expect(screen.getByRole('tab', { name: /entities\s*1/i })).toBeTruthy()
  })

  it('filters by kind when a tab is chosen', async () => {
    const calls = stub()
    renderPage()
    await userEvent.click(await screen.findByRole('tab', { name: /procedures/i }))
    await waitFor(() => expect(calls.some((c) => c.path.includes('kind=procedure'))).toBe(true))
    expect(await screen.findByText('Deploy checklist')).toBeTruthy()
    expect(screen.queryByText('Retry with backoff')).toBeNull()
  })
})

describe('SharedKnowledgePage cards', () => {
  it('does not repeat the title as the body', async () => {
    stub()
    renderPage()
    await screen.findByText('Acme Corp')
    expect(screen.getAllByText('Acme Corp')).toHaveLength(1)
  })

  it('hides the classification badge when unclassified and shows a real one', async () => {
    stub()
    renderPage()
    await screen.findByText('Acme Corp')
    expect(screen.queryByText(/unclassified/i)).toBeNull()
    expect(screen.queryByText(/unlabeled/i)).toBeNull()
    expect(screen.getByText('CUI')).toBeTruthy()
  })

  it('shows owner, contributors and promoted time', async () => {
    stub()
    renderPage()
    const card = (await screen.findByText('Retry with backoff')).closest('button') as HTMLElement
    expect(within(card).getByText(/Max/)).toBeTruthy()
    expect(within(card).getByText(/promoted/i)).toBeTruthy()
    expect(screen.getAllByText('Olivia').length).toBeGreaterThan(0)
  })
})

describe('SharedKnowledgePage drawer', () => {
  it('shows full content and provenance: who, when, classifier vs operator', async () => {
    stub()
    renderPage()
    await userEvent.click(await screen.findByText('Retry with backoff'))
    expect(await screen.findByText(/FULL BODY/)).toBeTruthy()
    expect(screen.getByText(/by classifier/i)).toBeTruthy()
    expect(screen.getByText(/97%/)).toBeTruthy()
    expect(screen.getByText(/jev-1\.13\.0/)).toBeTruthy()
    expect(screen.getByText(/by operator/i)).toBeTruthy()
    expect(screen.getByText('insight:ins-9')).toBeTruthy()
  })
})

describe('SharedKnowledgePage remove (demote)', () => {
  it('hides Remove for non-operators', async () => {
    stub()
    renderPage()
    await userEvent.click(await screen.findByText('Retry with backoff'))
    await screen.findByText(/FULL BODY/)
    expect(screen.queryByRole('button', { name: /^remove from fleet/i })).toBeNull()
  })

  it('requires a reason, warns it is permanent, then posts the reason', async () => {
    localStorage.setItem('arcui_operator_mode', '1')
    const calls = stub()
    renderPage()
    await userEvent.click(await screen.findByText('Retry with backoff'))
    await userEvent.click(await screen.findByRole('button', { name: /^remove from fleet/i }))
    expect(screen.getByText(/this is permanent/i)).toBeTruthy()
    expect(screen.getByText(/never be re-shared/i)).toBeTruthy()
    const confirm = screen.getByRole('button', { name: /remove permanently/i }) as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    await userEvent.type(screen.getByLabelText(/reason/i), 'Contains wrong advice')
    expect(confirm.disabled).toBe(false)
    await userEvent.click(confirm)
    await waitFor(() => {
      const post = calls.find((c) => c.method === 'POST')
      expect(post?.path).toBe('/api/team/knowledge/shared/ins-1/demote')
      expect(post?.body).toEqual({ reason: 'Contains wrong advice' })
    })
  })

  it('shows the server refusal verbatim', async () => {
    localStorage.setItem('arcui_operator_mode', '1')
    stub({ demoteStatus: 403 })
    renderPage()
    await userEvent.click(await screen.findByText('Retry with backoff'))
    await userEvent.click(await screen.findByRole('button', { name: /^remove from fleet/i }))
    await userEvent.type(screen.getByLabelText(/reason/i), 'why')
    await userEvent.click(screen.getByRole('button', { name: /remove permanently/i }))
    expect(await screen.findByText(/Operator role required/)).toBeTruthy()
  })
})

describe('SharedKnowledgePage include removed', () => {
  it('toggle asks for demoted cards and labels them removed with the reason', async () => {
    const calls = stub()
    renderPage()
    await screen.findByText('Retry with backoff')
    expect(screen.queryByText('Removed card')).toBeNull()
    await userEvent.click(screen.getByRole('checkbox', { name: /include removed/i }))
    await waitFor(() => expect(calls.some((c) => c.path.includes('include_demoted=1'))).toBe(true))
    expect(await screen.findByText('Removed card')).toBeTruthy()
    expect(screen.getByText(/was wrong/)).toBeTruthy()
  })
})
