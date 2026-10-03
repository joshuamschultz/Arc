// Knowledge > Connections, driven the way an operator uses it (connections sweep
// D11 and J-K1..J-K4): every problem reads in plain words with the action that
// fixes it, and every lifecycle step the server supports has a button.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ConnectionsBrowser } from '@/components/knowledge-connections'

const SOURCE = {
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

type Reply = { status?: number; body: unknown }
type Calls = Array<{ method: string; path: string }>

function stub(routes: (method: string, path: string) => Reply | undefined): Calls {
  const calls: Calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const method = (init?.method ?? 'GET').toUpperCase()
      calls.push({ method, path })
      const reply = routes(method, path) ?? { body: { items: [] } }
      return new Response(JSON.stringify(reply.body), {
        status: reply.status ?? 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
  return calls
}

function sources(...items: Array<Record<string, unknown>>) {
  return (method: string, path: string): Reply | undefined =>
    method === 'GET' && path.endsWith('/knowledge/connected-sources') ? { body: { items } } : undefined
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

async function openSheet(label: string) {
  fireEvent.click(await screen.findByText(label))
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('Knowledge connections', () => {
  it('turns Knowledge sync on from the page when the module is not active (J-K1)', async () => {
    const calls = stub((method, path) => {
      if (path.endsWith('/knowledge/connected-sources')) return { body: { items: [], status: 'degraded' } }
      if (method === 'POST' && path.endsWith('/knowledge/connected-data/activate'))
        return { body: { status: 'activated', detail: 'connected_data enabled' } }
      return undefined
    })
    wrap(<ConnectionsBrowser agentId="olivia" />)

    fireEvent.click(await screen.findByRole('button', { name: /turn on knowledge sync/i }))

    await screen.findByText(/knowledge sync is on/i)
    expect(calls).toContainEqual({ method: 'POST', path: '/api/agents/olivia/knowledge/connected-data/activate' })
    expect(screen.queryByText(/restart the agent/i)).toBeNull()
  })

  it('offers Relayout beside the other controls (J-K2)', async () => {
    const calls = stub((method, path) => {
      if (method === 'POST' && path.includes('/relayout'))
        return { body: { status: 'relayout_done', action: 'relayout', source_id: 'systems:drive', detail: '' } }
      return sources(SOURCE)(method, path)
    })
    wrap(<ConnectionsBrowser agentId="olivia" />)
    await openSheet('Systems Drive')

    fireEvent.click(await screen.findByRole('button', { name: /relayout/i }))

    await vi.waitFor(() =>
      expect(calls).toContainEqual({
        method: 'POST',
        path: '/api/agents/olivia/knowledge/sync/systems%3Adrive/relayout',
      }),
    )
  })

  it('says which store each connection reads from and previews the move (J-K3)', async () => {
    const calls = stub((method, path) => {
      if (method === 'POST' && path.endsWith('/knowledge/shared-migration'))
        return {
          body: {
            dry_run: true,
            items: [
              { connection_id: 'systems:drive', status: 'would_migrate', detail: '', documents: 738, adopted: 738, deduplicated: 0, skipped: 0 },
            ],
          },
        }
      return sources({ ...SOURCE, lane: 'migrating' }, { ...SOURCE, connection_id: 'wiki', label: 'Team wiki', lane: 'shared' })(method, path)
    })
    wrap(<ConnectionsBrowser agentId="olivia" />)

    expect(await screen.findByText(/waiting to move/i)).toBeTruthy()
    expect(screen.getByText(/^shared store$/i)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /preview the move/i }))

    expect(await screen.findByText(/will move 738 documents/i)).toBeTruthy()
    expect(calls).toContainEqual({ method: 'POST', path: '/api/agents/olivia/knowledge/shared-migration' })
  })

  it('explains a signed-out account in plain words with a Reconnect action (J-K4)', async () => {
    stub(
      sources({
        ...SOURCE,
        status: 'needs_attention',
        error_code: 'auth_required',
        detail: "Google credential unavailable (CREDENTIAL_RENEWAL_FAILED): 'systems' has no stored credential",
      }),
    )
    wrap(<ConnectionsBrowser agentId="olivia" />)
    await openSheet('Systems Drive')

    expect(await screen.findByText(/this account is signed out/i)).toBeTruthy()
    const reconnect = screen.getAllByRole('link', { name: /reconnect/i })[0]
    expect(reconnect.getAttribute('href')).toBe('/connections')
    expect(screen.queryByText(/CREDENTIAL_RENEWAL_FAILED/)).toBeNull()
    expect(screen.queryByText('auth_required')).toBeNull()
  })

  it('names a rate limit as a wait, not a failure', async () => {
    stub(sources({ ...SOURCE, status: 'idle', error_code: 'rate_limited', detail: 'rate_limited' }))
    wrap(<ConnectionsBrowser agentId="olivia" />)
    await openSheet('Systems Drive')

    expect(await screen.findByText(/asked arc to slow down/i)).toBeTruthy()
    expect(screen.queryByRole('link', { name: /reconnect/i })).toBeNull()
  })

  it('turns a 409 resource list into a Reconnect action (D11)', async () => {
    stub((method, path) => {
      if (path.endsWith('/resources'))
        return {
          status: 409,
          body: {
            error: 'This account is signed out. Reconnect it to see and choose what Arc may read.',
            action: 'reconnect',
          },
        }
      return sources(SOURCE)(method, path)
    })
    wrap(<ConnectionsBrowser agentId="olivia" />)
    await openSheet('Systems Drive')

    expect(await screen.findByText(/reconnect it to see and choose/i)).toBeTruthy()
    expect(screen.getAllByRole('link', { name: /reconnect/i }).length).toBeGreaterThan(0)
    expect(screen.queryByText(/HTTP 409/)).toBeNull()
  })

  it('shows a provider outage on the resource list as a retry, not a raw error', async () => {
    stub((method, path) => {
      if (path.endsWith('/resources'))
        return { status: 503, body: { error: 'source systems:drive could not be read' } }
      return sources(SOURCE)(method, path)
    })
    wrap(<ConnectionsBrowser agentId="olivia" />)
    await openSheet('Systems Drive')

    expect(await screen.findByText(/did not answer/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /try again/i })).toBeTruthy()
  })
})
