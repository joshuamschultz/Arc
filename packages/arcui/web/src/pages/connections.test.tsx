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

const bundle = (perAccount: boolean): CatalogBundle => ({
  name: 'google_workspace', display_name: 'Google Workspace', version: '1.0.0',
  description: 'Gmail, Calendar and Drive', attachment: 'cli', tier_floor: 'personal',
  approval_default: 'ask', knowledge_mode: 'source', knowledge_reason: '',
  secrets: perAccount ? [{ name: 'account', prompt: 'Google account', sensitive: false, value: '' }] : [],
  host_requires: [{ name: 'gog', instruction: 'install gog', satisfied: true }],
  tools: [], root: '/ext',
})

function wrap(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>,
  )
}

describe('BundleCard', () => {
  it('offers "Add an account" for a bundle that asks for an account', () => {
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
  last_success_at: null, last_notice: null, connect_kind: 'oauth', oauth_provider: 'google', knowledge_sync: [],
  ...over,
})

interface StubCall { url: string; method: string; body: unknown }

// Serves the page's reads and records every request made.
function stubApi(connections: ConnectorInstance[], opts: { appConfigured?: boolean } = {}) {
  const urls: string[] = []
  const calls: StubCall[] = []
  vi.stubGlobal('fetch', vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
    const path = String(request)
    urls.push(path)
    calls.push({
      url: path,
      method: init?.method ?? 'GET',
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined,
    })
    const json = (body: unknown) => new Response(JSON.stringify(body))
    if (path.includes('/api/team/roster')) return json({ agents: [] })
    if (path.includes('/api/connectors/catalog')) return json({ available: [bundle(true)], unreadable: [] })
    if (path.endsWith('/api/oauth-apps/google')) {
      return json({
        provider: 'google', configured: opts.appConfigured ?? true, client_id_hint: '1234…',
        redirect_uri: 'http://arc.local:8420/oauth/callback', console_url: 'https://console.cloud.google.com/',
      })
    }
    if (path.endsWith('/oauth/begin')) {
      return json({
        authorize_url: 'https://accounts.google.com/o/oauth2/auth?state=s1', state: 's1',
        redirect_mode: 'callback', expires_in: 600,
      })
    }
    if (path.endsWith('/api/oauth/complete')) return json(connections[0])
    if (path.endsWith('/auth')) {
      return json({
        instance: 'gmail-olivia', extension: 'google_workspace', extension_display_name: 'Google Workspace',
        credentials: [], reachable: true, detail: '', sign_in: 'expired',
        hosts: [{ name: 'gog' }],
      })
    }
    if (path.endsWith('/api/connections')) return json({ connections, extensions_roots: [] })
    return new Response(JSON.stringify({ error: 'nope' }), { status: 404 })
  }))
  return { urls, calls }
}

async function renderCard(connections: ConnectorInstance[], opts: { appConfigured?: boolean } = {}) {
  localStorage.setItem('arcui_operator_mode', '1')
  const { urls, calls } = stubApi(connections, opts)
  wrap(<ConnectionsPage />)
  const name = await screen.findByText(connections[0].instance, { selector: '[data-connection-card] span' })
  return { card: name.closest('[data-connection-card]') as HTMLElement, urls, calls }
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

  it('says no agent is running when the notice was undeliverable', async () => {
    const { card } = await renderCard([
      row({ last_notice: { kind: 'needs_you', delivered: false, channel: 'undeliverable', at: new Date().toISOString() } }),
    ])
    expect(within(card).getByText('Could not notify you: no agent is running')).toBeTruthy()
  })

  it('card never requests /auth on mount', async () => {
    const { urls } = await renderCard([needsYou({ agents: ['olivia'] })])
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(urls.some((u) => u.endsWith('/auth') || u.endsWith('/auth-status'))).toBe(false)
    expect(urls.some((u) => u.includes('/api/connected-data'))).toBe(false)
  })

  it('oauth connect opens provider and completes from pasted address', async () => {
    const open = vi.fn()
    vi.stubGlobal('open', open)
    const { card, calls } = await renderCard([needsYou()])
    await userEvent.click(within(card).getByRole('button', { name: 'Reconnect Google' }))
    await userEvent.click(await within(card).findByRole('button', { name: 'Reconnect' }))
    await waitFor(() =>
      expect(open).toHaveBeenCalledWith('https://accounts.google.com/o/oauth2/auth?state=s1', '_blank', 'noopener'),
    )
    expect(calls.find((c) => c.url.endsWith('/gmail-olivia/oauth/begin'))?.method).toBe('POST')
    expect(await within(card).findByText('Waiting for Google…')).toBeTruthy()
    await userEvent.click(within(card).getByText(/Didn't come back\?/))
    const pasted = 'http://arc.local:8420/oauth/callback?code=c1&state=s1'
    await userEvent.type(within(card).getByLabelText('Address you landed on'), pasted)
    await userEvent.click(within(card).getByRole('button', { name: /Finish connecting/ }))
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/api/oauth/complete'))).toBe(true))
    expect(calls.find((c) => c.url.endsWith('/api/oauth/complete'))?.body).toEqual({ redirect_url: pasted })
    await waitFor(() => expect(within(card).queryByText('Waiting for Google…')).toBeNull())
  })

  it('app setup panel shows redirect uri when app missing', async () => {
    const { card, calls } = await renderCard([needsYou()], { appConfigured: false })
    await userEvent.click(within(card).getByRole('button', { name: 'Reconnect Google' }))
    const redirect = (await within(card).findByLabelText('Redirect address')) as HTMLInputElement
    expect(redirect.value).toBe('http://arc.local:8420/oauth/callback')
    expect(within(card).getByText(/google-accounts\.md/)).toBeTruthy()
    expect((within(card).getByLabelText('Client secret') as HTMLInputElement).type).toBe('password')
    expect(within(card).queryByRole('button', { name: 'Reconnect' })).toBeNull()
    await userEvent.type(within(card).getByLabelText('Client ID'), 'cid')
    await userEvent.type(within(card).getByLabelText('Client secret'), 'sek')
    await userEvent.click(within(card).getByRole('button', { name: 'Save app' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({ client_id: 'cid', client_secret: 'sek' })
  })
})

describe('Add MCP server', () => {
  it('puts the button in the page header, not on a card, for an operator', async () => {
    const { card } = await renderCard([row()])
    const button = screen.getByRole('button', { name: /Add MCP server/ })
    expect(card.contains(button)).toBe(false)
    await userEvent.click(button)
    expect(await screen.findByLabelText('Server URL')).toBeTruthy()
  })

  it('is absent without operator controls', async () => {
    stubApi([row()])
    wrap(<ConnectionsPage />)
    await screen.findByText('gmail-olivia', { selector: '[data-connection-card] span' })
    expect(screen.queryByRole('button', { name: /Add MCP server/ })).toBeNull()
  })
})
