import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { OAuthConnectPanel } from '@/components/oauth-connect-panel'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

interface Call { url: string; method: string; body: unknown }

interface AppStub {
  provider?: string
  configured?: boolean
  redirectUri?: string
  redirectMode?: 'callback' | 'none'
}

function stubApp({
  provider = 'microsoft',
  configured = false,
  redirectUri = 'http://127.0.0.1:8420/oauth/callback',
  redirectMode = 'callback',
}: AppStub = {}): Call[] {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
    const url = String(request)
    const method = init?.method ?? 'GET'
    calls.push({ url, method, body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined })
    if (url.endsWith(`/api/oauth-apps/${provider}`) && method === 'GET') {
      return new Response(JSON.stringify({
        provider, configured, client_id_hint: '', console_url: 'https://entra.microsoft.com/',
        redirect_uri: redirectUri, tenant_required: provider === 'microsoft', tenant_id: '', cloud: '',
        clouds: provider === 'microsoft' ? [
          { id: 'global', label: 'Commercial / GCC' },
          { id: 'usgov', label: 'GCC High' },
          { id: 'dod', label: 'DoD' },
        ] : [],
      }))
    }
    if (url.includes('/oauth/begin')) {
      return new Response(JSON.stringify({
        authorize_url: 'https://provider.example/authorize', state: 's1',
        redirect_mode: redirectMode, expires_in: 600,
      }))
    }
    return new Response(JSON.stringify({ configured: true }))
  }))
  return calls
}

const stubMicrosoftApp = (configured: boolean) => stubApp({ configured })

function wrap(provider = 'microsoft') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <OAuthConnectPanel instance="work_mail" provider={provider} reconnect={false} onDone={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('Microsoft 365 app setup', () => {
  it('tells the operator exactly what to create in Entra', async () => {
    stubMicrosoftApp(false)
    wrap()
    const steps = await screen.findByText(/App registrations/)
    const list = steps.closest('[data-entra-steps]') as HTMLElement
    expect(list.textContent).toMatch(/Web/)
    expect(list.textContent).toMatch(/Mail\.Read, Mail\.Send, Calendars\.ReadWrite, Files\.Read/)
    expect(list.textContent).toMatch(/Grant admin consent/)
    expect(list.textContent).toMatch(/GCC/)
    expect((screen.getByLabelText('Redirect address') as HTMLInputElement).value).toBe(
      'http://127.0.0.1:8420/oauth/callback',
    )
  })

  it('asks for the tenant ID and the cloud, defaulting to Commercial / GCC', async () => {
    const calls = stubMicrosoftApp(false)
    wrap()
    const cloud = (await screen.findByLabelText('Cloud')) as HTMLSelectElement
    expect(cloud.value).toBe('global')
    expect(cloud.selectedOptions[0].textContent).toBe('Commercial / GCC')
    const save = screen.getByRole('button', { name: 'Save app' })
    await userEvent.type(screen.getByLabelText('Client ID'), 'aaaa-client')
    await userEvent.type(screen.getByLabelText('Client secret'), 'sek')
    expect((save as HTMLButtonElement).disabled).toBe(true)
    await userEvent.type(screen.getByLabelText('Tenant ID'), '11111111-2222-3333-4444-555555555555')
    await userEvent.click(save)
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({
      client_id: 'aaaa-client',
      client_secret: 'sek',
      tenant_id: '11111111-2222-3333-4444-555555555555',
      cloud: 'global',
    })
  })

  it('offers Connect once the app is set up', async () => {
    stubMicrosoftApp(true)
    wrap()
    expect(await screen.findByRole('button', { name: /Connect/ })).toBeTruthy()
    expect(screen.queryByLabelText('Tenant ID')).toBeNull()
  })
})

describe('Per-provider setup steps', () => {
  it('walks through Google with the exact redirect address', async () => {
    stubApp({ provider: 'google', redirectUri: 'https://arc.example.com/oauth/callback' })
    wrap('google')
    const steps = (await screen.findByText(/Credentials/)).closest('[data-provider-steps]') as HTMLElement
    expect(steps.textContent).toMatch(/Web application/)
    expect(steps.textContent).toMatch(/Authorized redirect URIs/)
    expect(steps.textContent).toMatch(/Production/)
    expect(steps.textContent).toContain('https://arc.example.com/oauth/callback')
  })

  it('walks through Microsoft with the exact redirect address and accurate localhost rules', async () => {
    stubApp({ provider: 'microsoft', redirectUri: 'https://arc.example.com/oauth/callback' })
    wrap('microsoft')
    const steps = (await screen.findByText(/App registrations/)).closest('[data-entra-steps]') as HTMLElement
    expect(steps.textContent).toContain('https://arc.example.com/oauth/callback')
    expect(steps.textContent).toMatch(/only accepts http for localhost/i)
    expect(steps.textContent).toMatch(/Manifest/)
    expect(steps.textContent).toMatch(/public address in Settings/)
  })

  it('walks through Atlassian with the exact callback URL', async () => {
    stubApp({ provider: 'atlassian', redirectUri: 'https://arc.example.com/oauth/callback' })
    wrap('atlassian')
    const steps = (await screen.findByText(/Developer console/)).closest('[data-provider-steps]') as HTMLElement
    expect(steps.textContent).toMatch(/OAuth 2\.0 \(3LO\)/)
    expect(steps.textContent).toMatch(/Jira API/)
    expect(steps.textContent).toMatch(/Confluence API/)
    expect(steps.textContent).toContain('https://arc.example.com/oauth/callback')
  })

  it('gives other providers a generic line', async () => {
    stubApp({ provider: 'dropbox' })
    wrap('dropbox')
    expect(
      await screen.findByText(/Register the redirect address above in the provider's developer console/),
    ).toBeTruthy()
  })

  it('never points at a repo file path', async () => {
    stubApp({ provider: 'google' })
    const { container } = wrap('google')
    await screen.findByText(/Credentials/)
    expect(container.textContent).not.toMatch(/docs\/runbooks/)
  })
})

describe('Loopback return address', () => {
  it('warns and links to Settings when the address is loopback', async () => {
    stubApp({ provider: 'google' })
    wrap('google')
    expect(await screen.findByText(/only works when your browser runs on the Arc computer/)).toBeTruthy()
    const link = screen.getByRole('link', { name: /Settings/ })
    expect(link.getAttribute('href')).toBe('/settings')
  })

  it('shows no warning for a public https address', async () => {
    stubApp({ provider: 'google', redirectUri: 'https://arc.example.com/oauth/callback' })
    wrap('google')
    await screen.findByText(/Credentials/)
    expect(screen.queryByText(/only works when your browser runs on the Arc computer/)).toBeNull()
  })
})

describe('Paste-back flow', () => {
  function stubOrigin(origin: string) {
    vi.stubGlobal('location', { ...window.location, origin })
  }

  it('explains what will happen before Connect', async () => {
    stubApp({ provider: 'google', configured: true, redirectUri: 'https://arc.example.com/oauth/callback' })
    stubOrigin('https://arc.example.com')
    wrap('google')
    expect(await screen.findByText(/Google opens in a new tab/)).toBeTruthy()
    expect(screen.getByText(/https:\/\/arc\.example\.com\/oauth\/callback/)).toBeTruthy()
    expect(screen.queryByText(/can't reach that address/)).toBeNull()
  })

  it('opens the paste box after Connect when the browser cannot reach the redirect address', async () => {
    stubApp({ provider: 'google', configured: true })
    stubOrigin('http://192.168.1.5:8420')
    vi.stubGlobal('open', vi.fn())
    wrap('google')
    expect(await screen.findByText(/This browser can't reach that address/)).toBeTruthy()
    await userEvent.click(screen.getByRole('button', { name: /Connect/ }))
    const box = await screen.findByLabelText('Address you landed on')
    expect((box.closest('details') as HTMLDetailsElement).open).toBe(true)
  })

  it('keeps the paste box collapsed when the redirect is reachable', async () => {
    stubApp({ provider: 'google', configured: true, redirectUri: 'http://192.168.1.5:8420/oauth/callback' })
    stubOrigin('http://192.168.1.5:8420')
    vi.stubGlobal('open', vi.fn())
    wrap('google')
    await userEvent.click(await screen.findByRole('button', { name: /Connect/ }))
    const summary = await screen.findByText(/Didn't come back/)
    expect((summary.closest('details') as HTMLDetailsElement).open).toBe(false)
  })
})
