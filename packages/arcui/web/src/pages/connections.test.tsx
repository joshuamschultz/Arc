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
  tools: [], root: '/ext', auto_installable: true, oauth_provider: '',
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
  last_success_at: null, last_notice: null, connect_kind: 'oauth', oauth_provider: 'google', app_missing: false, knowledge_sync: [],
  ...over,
})

interface StubCall { url: string; method: string; body: unknown }

// Serves the page's reads and records every request made.
interface StubOptions { appConfigured?: boolean; bundles?: CatalogBundle[]; signIn?: string }

function stubApi(connections: ConnectorInstance[], opts: StubOptions = {}) {
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
    if (path.includes('/api/connectors/catalog')) return json({ available: opts.bundles ?? [bundle(true)], unreadable: [] })
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
    if (path.endsWith('/probe')) return json({ reachable: true, detail: 'Reached GitHub as josh.' })
    if (path.endsWith('/authorize')) {
      return json({ sign_in: 'signed_in', reachable: true, detail: 'ok', command: '' })
    }
    if (path.endsWith('/auth-status')) {
      return json({ sign_in: opts.signIn ?? 'signed_out', reachable: false, detail: '', command: '' })
    }
    if (path.endsWith('/auth') && init?.method === 'PUT') return json({ instance: 'x', fields: ['token'] })
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

async function renderCard(connections: ConnectorInstance[], opts: StubOptions = {}) {
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

  it('a slow remove shows Removing… on the button, not a stuck pink button', async () => {
    const { card } = await renderCard([row()])
    const serve = vi.mocked(fetch).getMockImplementation()!
    let finish: (response: Response) => void = () => {}
    vi.mocked(fetch).mockImplementation((request, init) =>
      init?.method === 'DELETE'
        ? new Promise<Response>((resolve) => { finish = resolve })
        : serve(request, init),
    )
    await userEvent.click(within(card).getByRole('button', { name: /Advanced/ }))
    await userEvent.click(await screen.findByRole('menuitem', { name: /Remove/ }))
    await userEvent.click(within(card).getByRole('button', { name: 'Confirm remove' }))

    expect(await within(card).findByRole('button', { name: 'Removing…' })).toBeTruthy()

    finish(new Response(JSON.stringify({
      instance: 'gmail-olivia', removed_secrets: [], removed_config: true, removed_state: true,
      activations: [{ agent: 'a', status: 'activation_pending', revision: 1, tools: [], detail: '' }],
    })))
    await waitFor(() => expect(within(card).queryByRole('button', { name: 'Removing…' })).toBeNull())
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

describe('?connection= deep link', () => {
  it('highlights and scrolls to the named connection card', async () => {
    stubApi([row({ instance: 'gmail-a' }), row({ instance: 'gmail-b' })])
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter initialEntries={['/connections?connection=gmail-b']}>
          <ConnectionsPage />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    const label = await screen.findByText('gmail-b', { selector: '[data-connection-card] span' })
    const card = label.closest('[data-connection-card]') as HTMLElement
    const other = screen.getByText('gmail-a', { selector: '[data-connection-card] span' })
    expect(card.className).toContain('ring-2')
    expect(other.closest('[data-connection-card]')!.className).not.toContain('ring-2')
    await waitFor(() => expect(scroll).toHaveBeenCalled())
  })
})

describe('ConnectionsPage mobile layout', () => {
  it('reflows cards by available width and lets action rows wrap', async () => {
    const { card } = await renderCard([row(), row({ instance: 'gmail-two' })])
    const grid = card.parentElement as HTMLElement
    // Auto-fit columns collapse to one at medium widths; a fixed 2-up grid clipped the second column.
    expect(grid.className).toContain('minmax(min(100%')
    expect(grid.className).not.toMatch(/(^|\s)md:grid-cols-2/)
    expect(card.className).toContain('min-w-0')
    const actions = within(card).getByRole('button', { name: /Advanced/ }).parentElement as HTMLElement
    expect(actions.className).toContain('flex-wrap')
    const body = actions.closest('.p-4') as HTMLElement
    expect(body.className).toContain('flex-wrap')
  })
})

const githubBundle = (): CatalogBundle => ({
  ...bundle(false), name: 'github', display_name: 'GitHub', attachment: 'cli',
  secrets: [{ name: 'token', prompt: 'A fine-grained personal access token', sensitive: true, value: '' }],
  host_requires: [],
})

const readwiseBundle = (): CatalogBundle => ({
  ...bundle(false), name: 'readwise_reader', display_name: 'Readwise Reader', attachment: 'cli',
  secrets: [],
  host_requires: [{ name: 'readwise', instruction: 'npm i -g @readwise/cli', satisfied: false }],
  auto_installable: false,
})

describe('Reconnect by connection kind (J-U6, J-U9)', () => {
  it('a token connection opens the re-auth form, which stores the pasted token', async () => {
    const github = needsYou({
      instance: 'gh', extension: 'github', extension_display_name: 'GitHub',
      connect_kind: 'token', oauth_provider: '', action_label: 'Reconnect GitHub',
    })
    const { card, calls } = await renderCard([github], { bundles: [githubBundle()] })

    await userEvent.click(within(card).getByRole('button', { name: 'Reconnect GitHub' }))

    expect(await screen.findByText('Re-authenticate gh')).toBeTruthy()
    expect(screen.queryByText(/keeps its own sign-in/)).toBeNull()
    await userEvent.type(await screen.findByLabelText('token'), 'ghp_new')
    await userEvent.click(screen.getByRole('button', { name: 'Replace credentials' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({ secrets: { token: 'ghp_new' } })
    expect(await screen.findByText('Reached GitHub as josh.')).toBeTruthy()
    // The card re-reads its stored status once the credential is stored and proved.
    await waitFor(() =>
      expect(calls.filter((c) => c.url.endsWith('/api/connections')).length).toBeGreaterThan(1),
    )
  })

  it('a host-login connection leads with a token box and never shows a command to copy', async () => {
    const readwise = needsYou({
      instance: 'rw', extension: 'readwise_reader', extension_display_name: 'Readwise Reader',
      connect_kind: 'host_login', oauth_provider: '', action_label: 'Reconnect Readwise Reader',
    })
    const { card, calls } = await renderCard([readwise], { bundles: [readwiseBundle()] })

    await userEvent.click(within(card).getByRole('button', { name: 'Reconnect Readwise Reader' }))

    const box = await within(card).findByLabelText('Access token')
    expect(within(card).queryByText(/terminal/)).toBeNull()
    await userEvent.type(box, 'rw-token')
    await userEvent.click(within(card).getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/rw/authorize'))).toBe(true))
    expect(calls.find((c) => c.url.endsWith('/rw/authorize'))?.body).toEqual({ token: 'rw-token' })
  })

  it('a card whose sign-in app is missing says so and opens the app form', async () => {
    const { card } = await renderCard(
      [needsYou({
        app_missing: true, reason_text: 'Set up the Google sign-in app first',
        action_label: 'Set up Google sign-in app',
      })],
      { appConfigured: false },
    )
    expect(within(card).getByText(/Set up the Google sign-in app first/)).toBeTruthy()

    await userEvent.click(within(card).getByRole('button', { name: 'Set up Google sign-in app' }))

    expect(await within(card).findByLabelText('Client ID')).toBeTruthy()
  })

  it('a new or changed tool contract offers an Approve button that approves', async () => {
    const { card, calls } = await renderCard([
      needsYou({
        reason_code: 'contract_changed', reason_text: '2 tool(s) are new or changed; approve them',
        action: 'approve', action_label: 'Approve new or changed tools',
      }),
    ])
    await userEvent.click(within(card).getByRole('button', { name: 'Approve new or changed tools' }))
    await waitFor(() => expect(calls.some((c) => c.url.endsWith('/approve'))).toBe(true))
  })
})

describe('the Install button is only offered when it can succeed (D18)', () => {
  it('a bundle Arc can never install gets a plain sentence and no Install wording', () => {
    wrap(<BundleCard bundle={readwiseBundle()} connectedCount={0} operatorMode onConnect={() => {}} />)
    expect(screen.getByText(/cannot install/i)).toBeTruthy()
    expect(screen.queryByText(/can install it when you connect/)).toBeNull()
    expect(screen.queryByText(/npm i -g/)).toBeNull()
  })
})
