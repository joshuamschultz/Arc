// The agent Knowledge page folded nine tabs into three: Sources (with the index
// health strip), Browse (one picker, then the view that fits the source), and
// Guides. These tests drive each old tab's job through its new home so nothing
// that worked is lost:
//   Sources       -> Sources tab                     Index health -> strip on Sources
//   Repository    -> Browse (file source: summary)   Documents    -> Browse (file source)
//   Blob folders  -> Browse (file source: folders)   Explorer     -> Browse (datastore)
//   Datastore     -> Browse (datastore): tables + "Look up a record"
//   Provenance    -> Browse > Advanced > "Trace an item id"
//   Profile review-> Needs-you inbox (see needs-you-inbox.profile.test.tsx)
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ConnectionsBrowser } from '@/components/knowledge-connections'

const FILE_SOURCE = {
  connection_id: 'systems:drive',
  source_id: 'source-1',
  source_kind: 'google_drive',
  label: 'Systems Drive',
  status: 'complete',
  detail: '',
  pages: 3,
  bytes_processed: 4096,
  error_code: null,
  last_synced_at: '2026-10-03T10:00:00+00:00',
  documents_indexed: 12,
  allowed_homes: ['document'],
  lane: 'own',
}
const DB_SOURCE = {
  ...FILE_SOURCE,
  connection_id: 'sales-db',
  source_id: 'db-1',
  source_kind: 'postgres_database',
  label: 'Sales database',
  documents_indexed: 0,
}
const BROKEN_SOURCE = {
  ...FILE_SOURCE,
  connection_id: 'old-wiki',
  source_id: 'wiki-1',
  label: 'Old wiki',
  status: 'needs_attention',
  error_code: 'auth_required',
  last_synced_at: null,
}

const HEALTH = {
  item: {
    live: true,
    vec_extension: true,
    embedder_backend: 'local',
    embedder_live: true,
    embedder_dims: 384,
    detail: '',
    degraded_reasons: ['embedder_slow'],
    workspaces: [
      { workspace: 'w1', indexed_chunks: 40, embedded_chunks: 30, insight_triggers: 1 },
      { workspace: 'w2', indexed_chunks: 2, embedded_chunks: 2, insight_triggers: 0 },
    ],
  },
}

const INDEX_ROOT = {
  source_id: 'source-1',
  folder: '',
  present: true,
  verified: true,
  document_count: 14,
  markdown: '',
  error: null,
  guidance: null,
  entries: [
    { kind: 'folder', path: 'contracts', title: 'Contracts', summary: 'Signed customer deals', classification: 'internal', digest: 'd', count: 9 },
    { kind: 'document', path: 'readme.md', title: 'Read me', summary: 'How this drive is laid out', classification: 'internal', digest: 'd2', count: 0 },
  ],
}

type Calls = string[]

function stub(extra: (method: string, path: string) => unknown | undefined = () => undefined): Calls {
  const calls: Calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const method = (init?.method ?? 'GET').toUpperCase()
      calls.push(`${method} ${path}`)
      const reply = extra(method, path)
      let body: unknown = reply
      if (reply === undefined) {
        if (path.endsWith('/knowledge/connected-sources')) body = { items: [FILE_SOURCE, DB_SOURCE, BROKEN_SOURCE] }
        else if (path.endsWith('/knowledge/index-health')) body = HEALTH
        else body = { items: [] }
      }
      return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }),
  )
  return calls
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

beforeAll(() => {
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.releasePointerCapture ??= () => {}
  Element.prototype.scrollIntoView ??= () => {}
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function openTab(name: string) {
  await userEvent.click(screen.getByRole('tab', { name }))
}

async function pickSource(label: string) {
  await userEvent.click(screen.getByRole('combobox'))
  await userEvent.click(await screen.findByRole('option', { name: label }))
}

describe('Knowledge connections: three tabs', () => {
  it('shows Sources, Browse and Guides, and none of the old nine', async () => {
    stub()
    wrap(<ConnectionsBrowser agentId="olivia" />)

    const names = screen.getAllByRole('tab').map((t) => t.textContent)
    expect(names).toEqual(['Sources', 'Browse', 'Guides'])
    for (const gone of ['Explorer', 'Repository', 'Documents', 'Datastore', 'Blob folders', 'Provenance', 'Profile review', 'Index health']) {
      expect(screen.queryByRole('tab', { name: gone })).toBeNull()
    }
  })
})

describe('Sources tab (old Sources + Index health)', () => {
  it('lists each source with counts, last sync and what went wrong, under a health strip', async () => {
    stub()
    wrap(<ConnectionsBrowser agentId="olivia" />)

    const strip = await screen.findByRole('region', { name: 'Index health' })
    expect(within(strip).getByText('42')).toBeTruthy() // 40 + 2 chunks indexed
    expect(within(strip).getByText('32')).toBeTruthy() // 30 + 2 embedded
    expect(within(strip).getByText('embedder_slow')).toBeTruthy()

    expect((await screen.findAllByText(/12 documents/)).length).toBeGreaterThan(0)
    expect(screen.getAllByText('2026-10-03T10:00:00+00:00').length).toBeGreaterThan(0)
    expect(screen.getByText('Never')).toBeTruthy()
    expect(screen.getByText('Signed out')).toBeTruthy()
    // The separate Index health tab is gone, so the strip is the only place it lives.
    expect(screen.queryByRole('tab', { name: /index health/i })).toBeNull()
  })

  it('keeps the per-source controls reachable (open a source, run a sync)', async () => {
    const calls = stub()
    wrap(<ConnectionsBrowser agentId="olivia" />)
    fireEvent.click(await screen.findByText('Systems Drive'))

    fireEvent.click(await screen.findByRole('button', { name: /initial sync/i }))

    await vi.waitFor(() =>
      expect(calls).toContain('POST /api/agents/olivia/knowledge/sync/systems%3Adrive/sync'),
    )
  })
})

describe('Browse tab: a file or document source (old Repository, Documents, Blob folders)', () => {
  function fileRoutes(method: string, path: string): unknown | undefined {
    if (path.includes('/knowledge/sources/source-1/index')) {
      return path.includes('folder=contracts')
        ? { ...INDEX_ROOT, folder: 'contracts', document_count: 9, entries: [{ kind: 'document', path: 'contracts/acme.md', title: 'Acme deal', summary: 'Renewal terms', classification: 'internal', digest: 'd3', count: 0 }] }
        : INDEX_ROOT
    }
    if (path.includes('/knowledge/documents'))
      return {
        items: [
          { chunk_id: 'c1', text: 'Quarterly plan for the team', pointer: 'plans/q4.md', source_id: 'source-1', score: 0.91, classification: 'internal', provenance: ['drive'] },
        ],
      }
    if (path.includes('/knowledge/blob-folders'))
      return { items: [{ slug: 'b1', name: 'Scans folder', entity_type: 'folder', classification: 'internal', confidence: 1, importance: 5, source: 'source-1', links_to: [], facts: ['412 scanned pages'], tags: [], aliases: [] }] }
    if (method === 'GET' && path.includes('/knowledge/provenance/'))
      return { items: [{ source: 'Systems Drive', external_id: 'ext-77', classification: 'internal' }] }
    return undefined
  }

  it('shows what the source is for, its folders and its documents in one view with one picker', async () => {
    stub(fileRoutes)
    wrap(<ConnectionsBrowser agentId="olivia" />)
    await openTab('Browse')
    await pickSource('Systems Drive')

    expect(await screen.findByText(/this source holds: contracts, read me\./i)).toBeTruthy()
    expect(screen.getByText('14 documents')).toBeTruthy()
    expect(screen.getByText('verified')).toBeTruthy()
    expect(await screen.findByRole('button', { name: 'Contracts' })).toBeTruthy()
    expect(screen.getByText('Signed customer deals')).toBeTruthy()
    expect(await screen.findByText('Quarterly plan for the team')).toBeTruthy()
    expect(await screen.findByText('Scans folder')).toBeTruthy()
    expect(screen.getByText('412 scanned pages')).toBeTruthy()
    // One picker only: no second source select for documents or folders.
    expect(screen.getAllByRole('combobox')).toHaveLength(1)
  })

  it('opens a folder in place and goes back up', async () => {
    const calls = stub(fileRoutes)
    wrap(<ConnectionsBrowser agentId="olivia" />)
    await openTab('Browse')
    await pickSource('Systems Drive')

    fireEvent.click(await screen.findByRole('button', { name: 'Contracts' }))

    expect(await screen.findByText('Renewal terms')).toBeTruthy()
    expect(calls.some((c) => c.includes('/sources/source-1/index?folder=contracts'))).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Up' }))
    expect(await screen.findByText('Signed customer deals')).toBeTruthy()
  })

  it('searches documents by typing and shows the match score', async () => {
    const calls = stub(fileRoutes)
    wrap(<ConnectionsBrowser agentId="olivia" />)
    await openTab('Browse')
    await pickSource('Systems Drive')

    fireEvent.change(await screen.findByRole('textbox', { name: /search documents and folders/i }), { target: { value: 'plan' } })

    await vi.waitFor(() => expect(calls.some((c) => c.includes('/knowledge/documents?source=source-1&q=plan'))).toBe(true))
    expect(await screen.findByText(/score 0\.910/)).toBeTruthy()
  })

  it('says plainly when the index is missing, without a second empty box', async () => {
    stub((method, path) =>
      path.includes('/sources/source-1/index') ? { ...INDEX_ROOT, present: false, entries: [] } : fileRoutes(method, path),
    )
    wrap(<ConnectionsBrowser agentId="olivia" />)
    await openTab('Browse')
    await pickSource('Systems Drive')

    expect(await screen.findByText(/has not written an index yet/i)).toBeTruthy()
  })

  it('shows a tamper notice and never the contents of an index that failed verification', async () => {
    stub((method, path) =>
      path.includes('/sources/source-1/index')
        ? { ...INDEX_ROOT, verified: false, entries: [], guidance: 'Run a sync again.', error: 'bad_signature' }
        : fileRoutes(method, path),
    )
    wrap(<ConnectionsBrowser agentId="olivia" />)
    await openTab('Browse')
    await pickSource('Systems Drive')

    expect(await screen.findByText(/index could not be verified/i)).toBeTruthy()
    expect(screen.queryByText('Signed customer deals')).toBeNull()
  })
})

describe('Browse tab: a datastore (old Explorer + Datastore)', () => {
  const ROUTES = (_method: string, path: string): unknown | undefined => {
    if (path.endsWith('/connected-sources/db-1/tables'))
      return { items: [{ slug: 't1', name: 'invoices', entity_type: 'table', classification: 'internal', confidence: 1, importance: 5, source: 'db-1', links_to: [], facts: ['id', 'amount'], tags: [], aliases: [] }] }
    if (path.includes('/connected-sources/db-1/chunks?q='))
      return { items: [{ chunk_id: 'k2', text: 'Invoice 42 is overdue', source: 'invoices', classification: 'internal', truncated: false }], mode: path.includes('mode=vector') ? 'vector' : 'literal', degraded: path.includes('mode=vector'), query: 'overdue' }
    if (path.includes('/connected-sources/db-1/chunks'))
      return { items: [{ chunk_id: 'k1', text: 'Invoices hold what a customer owes', source: 'invoices', classification: 'internal', truncated: false }], total: 1, limit: 50, offset: 0 }
    if (path.includes('/knowledge/datastore?')) return { result: { id: 42, amount: 1999 } }
    return undefined
  }

  it('shows the tables and the indexed chunks together', async () => {
    stub(ROUTES)
    wrap(<ConnectionsBrowser agentId="olivia" />)
    await openTab('Browse')
    await pickSource('Sales database')

    expect(await screen.findByText('invoices', { selector: 'td' })).toBeTruthy()
    expect(await screen.findByText('Invoices hold what a customer owes')).toBeTruthy()
    expect(screen.getAllByRole('combobox').length).toBeGreaterThan(0)
    // A datastore does not get the folder/document view.
    expect(screen.queryByText('What this source is for')).toBeNull()
  })

  it('searches chunks by exact words or by meaning, and says when meaning search is off', async () => {
    const calls = stub(ROUTES)
    wrap(<ConnectionsBrowser agentId="olivia" />)
    await openTab('Browse')
    await pickSource('Sales database')

    fireEvent.change(await screen.findByRole('textbox', { name: /search chunks/i }), { target: { value: 'overdue' } })
    expect(await screen.findByText('Invoice 42 is overdue')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'By meaning' }))

    expect(await screen.findByText(/search by meaning is unavailable/i)).toBeTruthy()
    expect(calls.some((c) => c.includes('q=overdue&mode=vector'))).toBe(true)
  })

  it('looks up one record from a "Look up a record" panel', async () => {
    const calls = stub(ROUTES)
    wrap(<ConnectionsBrowser agentId="olivia" />)
    await openTab('Browse')
    await pickSource('Sales database')
    await screen.findByText('Look up a record')

    fireEvent.change(screen.getByRole('textbox', { name: 'Table name' }), { target: { value: 'invoices' } })
    fireEvent.change(screen.getByRole('textbox', { name: 'Record id' }), { target: { value: '42' } })

    await vi.waitFor(() =>
      expect(calls.some((c) => c.includes('/knowledge/datastore?source=db-1&op=get_record&table=invoices&pk_value=42'))).toBe(true),
    )
    expect(await screen.findByText(/1999/)).toBeTruthy()
  })
})

describe('Browse tab: Advanced', () => {
  it('traces an item id back to every source that claims it', async () => {
    const calls = stub((_method, path) =>
      path.includes('/knowledge/provenance/')
        ? { items: [{ source: 'Systems Drive', external_id: 'ext-77', classification: 'internal' }] }
        : undefined,
    )
    wrap(<ConnectionsBrowser agentId="olivia" />)
    await openTab('Browse')

    const advanced = screen.getByText('Advanced').closest('details') as HTMLElement
    expect(within(advanced).getByText('Trace an item id')).toBeTruthy()
    fireEvent.change(within(advanced).getByRole('textbox', { name: 'Item id' }), { target: { value: 'item-9' } })

    expect(await within(advanced).findByText('ext-77')).toBeTruthy()
    expect(calls).toContain('GET /api/agents/olivia/knowledge/provenance/item-9')
  })

  it('has no Profile review anywhere on this page', async () => {
    stub()
    wrap(<ConnectionsBrowser agentId="olivia" />)
    await openTab('Browse')
    expect(screen.queryByText(/profile review/i)).toBeNull()
    expect(screen.queryByText(/profile facts/i)).toBeNull()
  })
})

describe('375 px layout', () => {
  it('lets the picker, search boxes and tables shrink or scroll instead of widening the page', async () => {
    stub((_method, path) => {
      if (path.includes('/sources/source-1/index')) return INDEX_ROOT
      if (path.endsWith('/connected-sources/db-1/tables'))
        return { items: [{ slug: 't1', name: 'invoices', entity_type: 'table', classification: 'internal', confidence: 1, importance: 5, source: 'db-1', links_to: [], facts: [], tags: [], aliases: [] }] }
      return undefined
    })
    wrap(<ConnectionsBrowser agentId="olivia" />)
    await openTab('Browse')

    expect(screen.getByRole('combobox').className).toContain('max-w-full')

    await pickSource('Systems Drive')
    const search = await screen.findByRole('textbox', { name: /search documents and folders/i })
    expect(search.className).toContain('w-full')
    expect((await screen.findByText('Signed customer deals')).closest('li')?.className).toContain('min-w-0')

    await pickSource('Sales database')
    const table = (await screen.findByText('invoices', { selector: 'td' })).closest('table')
    expect(table?.parentElement?.className).toContain('overflow-x-auto')
    for (const name of ['Table name', 'Record id']) {
      expect(screen.getByRole('textbox', { name }).className).toContain('w-full')
    }
  })
})
