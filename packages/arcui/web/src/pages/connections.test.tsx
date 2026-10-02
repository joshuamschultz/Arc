import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { BundleCard, ConnectionsPage } from '@/pages/connections'
import type { CatalogBundle, ConnectorInstance } from '@/lib/types'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

const bundle = (remote: boolean): CatalogBundle => ({
  name: 'google_workspace', display_name: 'Google Workspace', version: '1.0.0',
  description: 'Gmail, Calendar and Drive', attachment: 'cli', tier_floor: 'personal',
  approval_default: 'ask', knowledge_mode: 'source', knowledge_reason: '',
  secrets: [{ name: 'account', prompt: 'Google account', sensitive: false, value: '' }],
  host_requires: [{ name: 'gog', instruction: 'install gog', satisfied: true, remote_login: remote }],
  tools: [], root: '/ext',
})

function wrap(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>,
  )
}

describe('BundleCard', () => {
  it('offers "Add an account" for a remote-login bundle', () => {
    wrap(<BundleCard bundle={bundle(true)} connectedCount={1} operatorMode onConnect={() => {}} />)
    expect(screen.getByRole('button', { name: /Add an account/ })).toBeTruthy()
    expect(
      screen.getByText('Each account is its own connection. After adding it, click the sign-in button on its card.'),
    ).toBeTruthy()
  })

  it('keeps "Connect" for every other bundle', () => {
    wrap(<BundleCard bundle={bundle(false)} connectedCount={0} operatorMode onConnect={() => {}} />)
    expect(screen.getByRole('button', { name: /^Connect$/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Add an account/ })).toBeNull()
  })
})

const row = (over: Partial<ConnectorInstance> = {}): ConnectorInstance => ({
  instance: 'gmail-olivia', extension: 'google_workspace', extension_display_name: 'Google Workspace',
  knowledge_mode: 'source', knowledge_reason: '', approval: 'ask', agents: [],
  status: 'healthy', display_status: 'healthy', reason_code: null, reason_text: null,
  action: 'none', action_label: '', last_checked_at: new Date(Date.now() - 4 * 60_000).toISOString(),
  last_success_at: null, last_notice: null, connect_kind: 'remote_login', knowledge_sync: [],
  ...over,
})

// Serves the page's reads and records every URL requested.
function stubApi(connections: ConnectorInstance[]) {
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (request: RequestInfo | URL) => {
    const path = String(request)
    urls.push(path)
    const json = (body: unknown) => new Response(JSON.stringify(body))
    if (path.includes('/api/team/roster')) return json({ agents: [] })
    if (path.includes('/api/connectors/catalog')) return json({ available: [bundle(true)], unreadable: [] })
    if (path.endsWith('/auth')) {
      return json({
        instance: 'gmail-olivia', extension: 'google_workspace', extension_display_name: 'Google Workspace',
        credentials: [], reachable: true, detail: '', sign_in: 'expired',
        hosts: [{ name: 'gog', remote_login: true }],
      })
    }
    if (path.endsWith('/api/connections')) return json({ connections, extensions_roots: [] })
    return new Response(JSON.stringify({ error: 'nope' }), { status: 404 })
  }))
  return urls
}

async function renderCard(connections: ConnectorInstance[]) {
  localStorage.setItem('arcui_operator_mode', '1')
  const urls = stubApi(connections)
  wrap(<ConnectionsPage />)
  const name = await screen.findByText(connections[0].instance, { selector: '[data-connection-card] span' })
  return { card: name.closest('[data-connection-card]') as HTMLElement, urls }
}

const needsYou = (over: Partial<ConnectorInstance> = {}) =>
  row({
    status: 'needs_you', display_status: 'needs_you',
    reason_text: 'Google sign-in expired or was revoked',
    action: 'reconnect', action_label: 'Reconnect Google', ...over,
  })

describe('ConnectionCard health row', () => {
  it('one status chip and one primary action per card', async () => {
    const { card } = await renderCard([needsYou()])
    expect(card.querySelectorAll('[data-status-chip]')).toHaveLength(1)
    const primaries = within(card).getAllByRole('button').filter((b) => /Reconnect Google/.test(b.textContent ?? ''))
    expect(primaries).toHaveLength(1)
  })

  it('healthy row shows the relative check time and no primary action', async () => {
    const { card } = await renderCard([row()])
    expect(within(card).getByText(/Healthy · checked 4m ago/)).toBeTruthy()
    expect(within(card).queryByRole('button', { name: /Reconnect/ })).toBeNull()
  })

  it('advanced menu holds the other verbs', async () => {
    const { card } = await renderCard([row()])
    expect(screen.queryByRole('menuitem', { name: /Check now/ })).toBeNull()
    await userEvent.click(within(card).getByRole('button', { name: /Advanced/ }))
    for (const name of [/Check now/, /Doctor/, /Edit details/, /Approve tools/, /Remove/]) {
      expect(await screen.findByRole('menuitem', { name })).toBeTruthy()
    }
  })

  it('needs_you shows reason and the action label', async () => {
    const { card } = await renderCard([needsYou()])
    expect(within(card).getByText(/Needs you: Google sign-in expired or was revoked/)).toBeTruthy()
    expect(within(card).getByRole('button', { name: 'Reconnect Google' })).toBeTruthy()
  })

  it('syncing chip when a knowledge row is running', async () => {
    const { card } = await renderCard([
      row({
        display_status: 'syncing', agents: ['olivia'],
        knowledge_sync: [{
          agent: 'olivia', source_id: 'gmail-olivia', state: 'running', running: true,
          last_synced_at: null, pages: 12, error_code: null,
        }],
      }),
    ])
    expect(card.querySelector('[data-status-chip="syncing"]')).toBeTruthy()
    const syncRow = card.querySelector('[data-knowledge-row]') as HTMLElement
    expect(within(syncRow).getByText(/Never/)).toBeTruthy()
    expect(within(syncRow).getByText('Syncing')).toBeTruthy()
    expect(within(syncRow).getByText('12 pages')).toBeTruthy()
  })

  it('says so when the operator could not be notified', async () => {
    const { card } = await renderCard([
      row({ last_notice: { kind: 'needs_you', delivered: false, channel: 'telegram', at: new Date().toISOString() } }),
    ])
    expect(within(card).getByText('Could not notify you')).toBeTruthy()
  })

  it('card never requests /auth on mount', async () => {
    const { urls } = await renderCard([needsYou({ agents: ['olivia'] })])
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(urls.some((u) => u.endsWith('/auth') || u.endsWith('/auth-status'))).toBe(false)
    expect(urls.some((u) => u.includes('/api/connected-data'))).toBe(false)
  })

  it('primary Reconnect opens the sign-in panel for a remote-login connection', async () => {
    const { card, urls } = await renderCard([needsYou()])
    await userEvent.click(within(card).getByRole('button', { name: 'Reconnect Google' }))
    await waitFor(() => expect(urls.some((u) => u.endsWith('/auth'))).toBe(true))
    expect(await within(card).findByText(/Google stopped accepting the saved sign-in/)).toBeTruthy()
  })
})
