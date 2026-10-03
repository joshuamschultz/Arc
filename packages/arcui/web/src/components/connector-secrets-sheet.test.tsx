import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { ConnectorSecretsSheet } from '@/components/connector-secrets-sheet'
import type { CatalogBundle, ConnectorSecret } from '@/lib/types'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const WARNING = "Blank uses gog's built-in sign-in client, whose Google sign-ins expire after about 7 days."

const field = (over: Partial<ConnectorSecret> & { name: string }): ConnectorSecret => ({
  prompt: over.name, sensitive: false, value: '', ...over,
})

const bundle = (secrets: ConnectorSecret[]): CatalogBundle => ({
  name: 'google_workspace', display_name: 'Google Workspace', version: '1.0.0',
  description: 'Gmail, Calendar and Drive', attachment: 'cli', tier_floor: 'personal',
  approval_default: 'ask', knowledge_mode: 'source', knowledge_reason: '',
  secrets,
  host_requires: [{ name: 'gog', instruction: 'install gog', satisfied: true }],
  tools: [], root: '/ext', auto_installable: true, oauth_provider: '',
})

const google = () => bundle([
  field({ name: 'account', required: false }),
  field({ name: 'client', required: false, warning: WARNING }),
  field({ name: 'read_only', required: false, choices: ['yes', 'no'], default: 'yes' }),
])

function renderSheet(b: CatalogBundle) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ConnectorSecretsSheet bundle={b} agents={[]} open onOpenChange={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function fillNameAndAccount() {
  await userEvent.type(screen.getAllByRole('textbox')[0], 'gmail-olivia')
  await userEvent.type(screen.getByLabelText('account'), 'olivia@example.com')
}

const connect = () => screen.getByRole('button', { name: 'Connect' }) as HTMLButtonElement

it('lets an optional field stay blank', async () => {
  renderSheet(bundle([field({ name: 'account', required: false }), field({ name: 'client', required: false })]))
  await fillNameAndAccount()
  expect(connect().disabled).toBe(false)
})

it('still asks for a field the server does not mark optional', async () => {
  renderSheet(bundle([field({ name: 'account', required: false }), field({ name: 'client' })]))
  await fillNameAndAccount()
  expect(connect().disabled).toBe(true)
})

it('renders a choice field as a select preselected to its default', () => {
  renderSheet(google())
  const select = screen.getByRole('combobox', { name: 'read_only' }) as HTMLSelectElement
  expect(select.value).toBe('yes')
  expect([...select.options].map((o) => o.value)).toEqual(['yes', 'no'])
})

it('warns under a blank field and hides the warning once filled', async () => {
  renderSheet(google())
  expect(screen.getByText(WARNING)).toBeTruthy()
  await userEvent.type(screen.getByLabelText('client'), 'my-client')
  expect(screen.queryByText(WARNING)).toBeNull()
})

it('accepts a filled warned field', async () => {
  renderSheet(google())
  await fillNameAndAccount()
  await userEvent.type(screen.getByLabelText('client'), 'my-client')
  expect(connect().disabled).toBe(false)
})

it('does not demand the warned field on an ordinary bundle', async () => {
  renderSheet(bundle([field({ name: 'account', required: false }), field({ name: 'client', required: false, warning: WARNING })]))
  await fillNameAndAccount()
  expect(connect().disabled).toBe(false)
})

const dropbox = (): CatalogBundle => ({
  ...bundle([field({ name: 'refresh_token', sensitive: true, required: false, managed: true })]),
  name: 'dropbox', display_name: 'Dropbox', attachment: 'native', host_requires: [],
  oauth_provider: 'dropbox',
})

function stubInstall() {
  const calls: { url: string; method: string; body: unknown }[] = []
  vi.stubGlobal('fetch', vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
    const url = String(request)
    calls.push({
      url, method: init?.method ?? 'GET',
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined,
    })
    const json = (body: unknown) => new Response(JSON.stringify(body))
    if (url.endsWith('/api/connections') && init?.method === 'POST') {
      return json({ instance: 'box', extension: 'dropbox', tools: [], agents: [], activations: [] })
    }
    if (url.endsWith('/api/oauth-apps/dropbox')) {
      return json({
        provider: 'dropbox', configured: true, client_id_hint: 'abcd…',
        redirect_uri: 'http://arc.local:8420/oauth/callback', console_url: 'https://www.dropbox.com/developers/apps',
      })
    }
    return new Response(JSON.stringify({ error: 'nope' }), { status: 404 })
  }))
  return calls
}

it('the Dropbox add form can be submitted: the token Connect fills in is not on the form', async () => {
  stubInstall()
  renderSheet(dropbox())
  expect(screen.queryByLabelText('refresh_token')).toBeNull()
  await userEvent.type(screen.getAllByRole('textbox')[0], 'box')
  expect(connect().disabled).toBe(false)
})

it('adding a one-click connection goes straight into Connect and never probes it', async () => {
  const calls = stubInstall()
  renderSheet(dropbox())
  await userEvent.type(screen.getAllByRole('textbox')[0], 'box')

  await userEvent.click(connect())

  expect(await screen.findByText(/One more step: sign in to Dropbox/)).toBeTruthy()
  expect(await screen.findByRole('button', { name: 'Connect' })).toBeTruthy()
  expect(screen.queryByText(/did not answer/)).toBeNull()
  await waitFor(() => expect(calls.some((c) => c.url.endsWith('/api/oauth-apps/dropbox'))).toBe(true))
  expect(calls.some((c) => c.url.endsWith('/probe'))).toBe(false)
})

it('Readwise (an npm package Arc installs) shows the Install button', () => {
  localStorage.setItem('arcui_operator_mode', '1')
  const readwise: CatalogBundle = {
    ...bundle([]), name: 'readwise_reader', display_name: 'Readwise Reader', auto_installable: true,
    host_requires: [{ name: 'readwise', instruction: 'npm i -g @readwise/cli', satisfied: false }],
  }
  renderSheet(readwise)
  expect(screen.getByRole('button', { name: /Install on this computer/ })).toBeTruthy()
  expect(screen.queryByText(/cannot install it from here yet/)).toBeNull()
  expect(screen.queryByText(/npm i -g/)).toBeNull()
  localStorage.removeItem('arcui_operator_mode')
})

it('a bundle Arc can never install shows no Install button, only the honest sentence', () => {
  const pinless: CatalogBundle = {
    ...bundle([]), name: 'pinless', display_name: 'Pinless', auto_installable: false,
    host_requires: [{ name: 'pinless', instruction: 'npm i -g pinless', satisfied: false }],
  }
  renderSheet(pinless)
  expect(screen.queryByRole('button', { name: /Install on this computer/ })).toBeNull()
  expect(screen.getByText(/cannot install it from here yet/)).toBeTruthy()
  expect(screen.queryByText(/npm i -g/)).toBeNull()
})
