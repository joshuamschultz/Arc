import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { PublicAccessPanel } from '@/components/settings-view/public-access-panel'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

interface Call { url: string; method: string; body: unknown }

interface Stub {
  address?: Record<string, unknown>
  tls?: Record<string, unknown>
  putAddressStatus?: number
}

const ADDRESS = {
  public_base_url: null,
  redirect_uri: 'http://127.0.0.1:8420/oauth/callback',
  tier: 'personal',
  https_required: false,
  suggestions: [],
}
const TLS = {
  configured: false, active: false, required: false, subject: null, not_after: null, dns_names: [],
}

function stub(options: Stub = {}): Call[] {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
    const url = String(request)
    const method = init?.method ?? 'GET'
    calls.push({ url, method, body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined })
    if (url.endsWith('/api/settings/public-address')) {
      if (method === 'PUT' && options.putAddressStatus === 400) {
        return new Response(JSON.stringify({ error: 'https is required at this tier' }), { status: 400 })
      }
      if (method === 'PUT') {
        const sent = JSON.parse(String(init?.body)).public_base_url as string | null
        return new Response(JSON.stringify({ ...ADDRESS, ...options.address, public_base_url: sent }))
      }
      return new Response(JSON.stringify({ ...ADDRESS, ...options.address }))
    }
    if (url.endsWith('/api/settings/tls')) {
      if (method === 'PUT') {
        return new Response(JSON.stringify({ ...TLS, configured: true, subject: 'CN=arc', restart_required: true }))
      }
      if (method === 'DELETE') {
        return new Response(JSON.stringify({ ...TLS, restart_required: true }))
      }
      return new Response(JSON.stringify({ ...TLS, ...options.tls }))
    }
    return new Response('{}')
  }))
  return calls
}

function wrap(editable = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <PublicAccessPanel editable={editable} />
    </QueryClientProvider>,
  )
}

describe('Public address', () => {
  it('explains the address and shows the derived sign-in return address', async () => {
    stub()
    wrap()
    expect(await screen.findByText(/address people use to open this dashboard/i)).toBeTruthy()
    expect(((await screen.findByLabelText('Sign-in return address')) as HTMLInputElement).value).toBe(
      'http://127.0.0.1:8420/oauth/callback',
    )
  })

  it('saves the typed address', async () => {
    const calls = stub()
    wrap()
    await userEvent.type(await screen.findByLabelText('Public address'), 'https://arc.example.com')
    await userEvent.click(screen.getByRole('button', { name: 'Save address' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT' && c.url.endsWith('public-address'))?.body).toEqual({
      public_base_url: 'https://arc.example.com',
    })
  })

  it('clears the address by sending null', async () => {
    const calls = stub({ address: { public_base_url: 'https://arc.example.com' } })
    wrap()
    await userEvent.click(await screen.findByRole('button', { name: 'Clear address' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({ public_base_url: null })
  })

  it('shows the server error on a bad address', async () => {
    stub({ putAddressStatus: 400 })
    wrap()
    await userEvent.type(await screen.findByLabelText('Public address'), 'http://arc.example.com')
    await userEvent.click(screen.getByRole('button', { name: 'Save address' }))
    expect(await screen.findByText(/https is required at this tier/)).toBeTruthy()
  })

  it('says https is required when the tier needs it', async () => {
    stub({ address: { https_required: true, tier: 'federal' } })
    wrap()
    expect(await screen.findByText(/https is required at this tier/i)).toBeTruthy()
  })

  it('offers the Tailscale suggestion and fills the box without saving', async () => {
    const calls = stub({ address: { suggestions: [{ source: 'tailscale', url: 'https://box.tail.ts.net' }] } })
    wrap()
    await screen.findByText(/Tailscale serve detected/i)
    await userEvent.click(screen.getByRole('button', { name: 'Use https://box.tail.ts.net' }))
    expect((screen.getByLabelText('Public address') as HTMLInputElement).value).toBe('https://box.tail.ts.net')
    expect(calls.some((c) => c.method === 'PUT')).toBe(false)
  })

  it('offers the https address the browser is on, when it differs from the saved one', async () => {
    stub()
    vi.stubGlobal('location', { ...window.location, origin: 'https://here.example.com' })
    wrap()
    await userEvent.click(await screen.findByRole('button', { name: 'Use https://here.example.com' }))
    expect((screen.getByLabelText('Public address') as HTMLInputElement).value).toBe('https://here.example.com')
  })

  it('does not offer the browser address when it is plain http', async () => {
    stub()
    vi.stubGlobal('location', { ...window.location, origin: 'http://192.168.1.5:8420' })
    wrap()
    await screen.findByLabelText('Public address')
    expect(screen.queryByText(/address you're on now/i)).toBeNull()
  })

  it('hides the editing controls from a viewer', async () => {
    stub()
    wrap(false)
    await screen.findByLabelText('Sign-in return address')
    expect(screen.queryByRole('button', { name: 'Save address' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save certificate' })).toBeNull()
  })
})

describe('HTTPS certificate', () => {
  it('shows the certificate details', async () => {
    stub({
      tls: {
        configured: true, active: true, subject: 'CN=arc.example.com',
        not_after: '2027-01-01T00:00:00Z', dns_names: ['arc.example.com'],
      },
    })
    wrap()
    expect(await screen.findByText(/CN=arc\.example\.com/)).toBeTruthy()
    expect(screen.getByText(/Names: arc\.example\.com/)).toBeTruthy()
  })

  it('explains when a certificate is not needed, without terminal commands', async () => {
    stub()
    wrap()
    const note = await screen.findByText(/reverse proxy/i)
    expect(note.textContent).toMatch(/public address/)
  })

  it('saves the certificate and clears the private key box', async () => {
    const calls = stub()
    wrap()
    await userEvent.type(await screen.findByLabelText('Certificate (PEM)'), 'CERT')
    const key = screen.getByLabelText('Private key (PEM)') as HTMLTextAreaElement
    await userEvent.type(key, 'KEY')
    await userEvent.click(screen.getByRole('button', { name: 'Save certificate' }))
    await waitFor(() => expect(key.value).toBe(''))
    expect(calls.find((c) => c.method === 'PUT' && c.url.endsWith('/tls'))?.body).toEqual({
      cert_pem: 'CERT', key_pem: 'KEY',
    })
    expect(await screen.findByText(/restart/i)).toBeTruthy()
  })

  it('shows a restart note when configured differs from active', async () => {
    stub({ tls: { configured: true, active: false } })
    wrap()
    expect(await screen.findByText(/Restart the dashboard/i)).toBeTruthy()
  })

  it('removes the certificate', async () => {
    const calls = stub({ tls: { configured: true, active: true } })
    wrap()
    await userEvent.click(await screen.findByRole('button', { name: 'Remove certificate' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true))
  })

  it('says a certificate is required at the federal tier and offers no removal', async () => {
    stub({ tls: { configured: true, active: true, required: true } })
    wrap()
    expect(await screen.findByText(/required at this tier/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Remove certificate' })).toBeNull()
  })

  it('shows the server error when the certificate is bad', async () => {
    const calls = stub()
    wrap()
    vi.stubGlobal('fetch', vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const url = String(request)
      if (url.endsWith('/tls') && init?.method === 'PUT') {
        return new Response(JSON.stringify({ error: 'key does not match certificate' }), { status: 400 })
      }
      return new Response(JSON.stringify({ ...TLS, ...ADDRESS }))
    }))
    await userEvent.type(await screen.findByLabelText('Certificate (PEM)'), 'C')
    await userEvent.type(screen.getByLabelText('Private key (PEM)'), 'K')
    await userEvent.click(screen.getByRole('button', { name: 'Save certificate' }))
    expect(await screen.findByText(/key does not match certificate/)).toBeTruthy()
    expect(calls.length).toBeGreaterThan(0)
  })
})
