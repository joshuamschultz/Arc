// Item 20 P20-6 — the Audit screen reads the verified ledger and its causal chain.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { SecurityPage } from '@/pages/security'
import type { AuditEvent } from '@/lib/types'

// Radix popper primitives (the identity hover card) measure with ResizeObserver.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

const agentRow: AuditEvent = {
  seq: 4,
  ts: '2026-10-01T10:00:00+00:00',
  actor_did: 'did:arc:t:olivia/1',
  action: 'policy.evaluate',
  action_label: 'Tool policy check',
  target: 'memory.write',
  outcome: 'allow',
  decision: 'Allowed',
  event_hash: 'h4',
  signature: 's4',
  signer: 'fp-agent',
  verified: true,
  chain: 'audit-chain-olivia',
  initiator: 'agent',
  initiator_id: 'did:arc:t:olivia/1',
  run_id: 'run-1',
  tool_call_id: 'tc-1',
  causal: { initiator: 'agent', initiator_id: 'did:arc:t:olivia/1', run_id: 'run-1', tool_call_id: 'tc-1' },
}

const uiRow: AuditEvent = {
  seq: 2,
  ts: '2026-10-01T09:00:00+00:00',
  actor_did: 'did:arc:ui:session:abc',
  action: 'task.cancel',
  action_label: 'Task cancelled',
  target: 'task:1',
  outcome: 'applied',
  event_hash: 'h2',
  signature: 's2',
  signer: 'fp-op',
  verified: false,
  chain: 'audit-chain-arcui',
  initiator: 'ui_session',
  initiator_id: 'did:arc:ui:session:abc',
  causal: { initiator: 'ui_session', initiator_id: 'did:arc:ui:session:abc' },
}

const brokenRow: AuditEvent = {
  seq: 9,
  ts: '2026-10-01T08:00:00+00:00',
  actor_did: 'did:arc:system:ingest',
  action: 'audit.chain.broken',
  target: 'audit-chain-olivia',
  outcome: 'broken',
  verified: false,
}

function stubServer(events: AuditEvent[] = [agentRow, uiRow, brokenRow]) {
  const paths: string[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      paths.push(`${init?.method ?? 'GET'} ${path}`)
      const json = (body: unknown) =>
        new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
      if (path.startsWith('/api/team/audit/reverify')) return json({ events: 3, verified: 3, broken: 0 })
      if (path.startsWith('/api/team/audit')) {
        return json({ events, totals: { total: 120, verified: 118, broken: 2 } })
      }
      return json({})
    }),
  )
  return paths
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TooltipProvider>
          <SecurityPage />
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('SecurityPage ledger header', () => {
  it('shows the whole ledger totals, not the page', async () => {
    stubServer()
    renderPage()
    await waitFor(() => expect(screen.getByTestId('ledger-total').textContent).toBe('120'))
    expect(screen.getByTestId('ledger-verified').textContent).toBe('118')
    expect(screen.getByTestId('ledger-broken').textContent).toBe('2')
  })
})

describe('SecurityPage rows', () => {
  it('names the initiator kind and id, with who signed as the secondary line', async () => {
    stubServer()
    renderPage()
    const row = (await screen.findByText('Task cancelled')).closest('tr') as HTMLElement
    expect(within(row).getByText(/UI session/)).toBeTruthy()
    expect(within(row).getByText('signed by operator')).toBeTruthy()
    const agentRowEl = screen.getByText('Tool policy check').closest('tr') as HTMLElement
    expect(within(agentRowEl).getByText(/Agent/)).toBeTruthy()
    expect(within(agentRowEl).getByText('signed by agent')).toBeTruthy()
  })

  it('links a row to its run and tool call', async () => {
    stubServer()
    renderPage()
    const row = (await screen.findByText('Tool policy check')).closest('tr') as HTMLElement
    const run = within(row).getByRole('link', { name: /run run-1/i })
    expect(run.getAttribute('href')).toBe('/arcrun?run=run-1')
    expect(within(row).getByRole('link', { name: /tool call tc-1/i })).toBeTruthy()
  })
})

describe('SecurityPage causal filters', () => {
  it('sends the typed filters to the server', async () => {
    const paths = stubServer()
    renderPage()
    await screen.findByText('Tool policy check')
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Run ID'), 'run-1')
    await user.type(screen.getByLabelText('Tool call ID'), 'tc-1')
    await user.selectOptions(screen.getByLabelText('Initiator'), 'agent')
    await user.click(screen.getByRole('button', { name: /apply filters/i }))
    const last = paths.filter((p) => p.startsWith('GET /api/team/audit?')).at(-1) ?? ''
    expect(last).toContain('run_id=run-1')
    expect(last).toContain('tool_call_id=tc-1')
    expect(last).toContain('initiator=agent')
  })
})

describe('SecurityPage row drawer', () => {
  it('shows the full causal chain and the verified state', async () => {
    stubServer()
    renderPage()
    const user = userEvent.setup()
    await user.click(await screen.findByText('Tool policy check'))
    const chain = await screen.findByRole('region', { name: /causal chain/i })
    expect(within(chain).getByText('run-1')).toBeTruthy()
    expect(within(chain).getByText('tc-1')).toBeTruthy()
    expect(screen.getByText(/verified against a trusted key/i)).toBeTruthy()
  })

  it('names the seq where a chain broke', async () => {
    stubServer()
    renderPage()
    const user = userEvent.setup()
    await user.click((await screen.findAllByText('audit.chain.broken'))[0])
    expect(await screen.findByText(/chain broken at seq 9/i)).toBeTruthy()
  })

  it('offers an operator a re-verify of the ledger', async () => {
    localStorage.setItem('arcui_operator_mode', '1')
    const paths = stubServer()
    renderPage()
    const user = userEvent.setup()
    await user.click(await screen.findByText('Task cancelled'))
    await user.click(await screen.findByRole('button', { name: /re-verify ledger/i }))
    expect(await screen.findByText(/3 of 3 verified/i)).toBeTruthy()
    expect(paths).toContain('POST /api/team/audit/reverify')
  })
})
