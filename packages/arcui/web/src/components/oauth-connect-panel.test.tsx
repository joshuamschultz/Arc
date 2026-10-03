import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { OAuthConnectPanel } from '@/components/oauth-connect-panel'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

interface Call { url: string; method: string; body: unknown }

function stubMicrosoftApp(configured: boolean): Call[] {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
    const url = String(request)
    calls.push({
      url,
      method: init?.method ?? 'GET',
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined,
    })
    if (url.endsWith('/api/oauth-apps/microsoft') && (init?.method ?? 'GET') === 'GET') {
      return new Response(JSON.stringify({
        provider: 'microsoft', configured, client_id_hint: '', console_url: 'https://entra.microsoft.com/',
        redirect_uri: 'http://127.0.0.1:8420/oauth/callback', tenant_required: true, tenant_id: '', cloud: '',
        clouds: [
          { id: 'global', label: 'Commercial / GCC' },
          { id: 'usgov', label: 'GCC High' },
          { id: 'dod', label: 'DoD' },
        ],
      }))
    }
    return new Response(JSON.stringify({ configured: true }))
  }))
  return calls
}

function wrap() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <OAuthConnectPanel instance="work_mail" provider="microsoft" reconnect={false} onDone={() => {}} />
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
