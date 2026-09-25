import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { RemoteSignInPanel } from '@/components/remote-sign-in-panel'
import type { ConnectorSignIn } from '@/lib/types'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

type Call = { path: string; method: string; body: unknown }

function stubServer({
  account = 'olivia@example.com',
  signIn = 'signed_out' as ConnectorSignIn,
  detail = 'no token stored',
  clientWarning = '',
  readOnly = 'yes' as string | null,
  begin = { status: 200, body: { instance: 'gmail-olivia', account: 'olivia@example.com', consent_url: 'https://accounts.google.com/o/oauth2/auth?x=1', expires_in: 600 } } as { status: number; body: unknown },
  complete = { status: 200, body: { sign_in: 'signed_in', reachable: true, detail: 'olivia@example.com' } } as { status: number; body: unknown },
} = {}) {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
    const path = String(request)
    const method = init?.method ?? 'GET'
    calls.push({ path, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    const json = (status: number, body: unknown) =>
      new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
    if (path.endsWith('/sign-in/begin')) return json(begin.status, begin.body)
    if (path.endsWith('/sign-in/complete')) return json(complete.status, complete.body)
    if (path.endsWith('/auth-status')) return json(200, { sign_in: signIn, reachable: true, detail })
    if (path.endsWith('/auth')) {
      return json(200, {
        instance: 'gmail-olivia', extension: 'google_workspace', extension_display_name: 'Google Workspace',
        credentials: [
          { name: 'account', prompt: 'Google account', sensitive: false, value: account, warning: '' },
          { name: 'client', prompt: 'OAuth client', sensitive: false, value: '', warning: clientWarning },
          ...(readOnly === null ? [] : [{
            name: 'read_only', prompt: 'Read only', sensitive: false, value: readOnly,
            choices: ['yes', 'no'], default: 'yes', warning: '',
          }]),
        ],
        reachable: true, detail, sign_in: signIn,
        hosts: [{ name: 'gog', remote_login: true }],
      })
    }
    return json(404, { error: 'not found' })
  }))
  return calls
}

function renderPanel(operatorMode = true, onEditDetails?: () => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RemoteSignInPanel instance="gmail-olivia" operatorMode={operatorMode} onEditDetails={onEditDetails} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('RemoteSignInPanel', () => {
  it('shows the account address', async () => {
    stubServer()
    renderPanel()
    expect(await screen.findByText('olivia@example.com')).toBeTruthy()
  })

  it.each([
    ['signed_in', /Working/],
    ['expired', /Reconnect needed — Google stopped accepting the saved sign-in/],
    ['signed_out', /Not signed in yet/],
    ['not_installed', /The Google program \(gog\) is not installed on this computer/],
    ['unknown', /Can't tell yet/],
  ] as const)('labels %s honestly', async (signIn, label) => {
    stubServer({ signIn, detail: `detail for ${signIn}` })
    renderPanel()
    expect(await screen.findByText(label)).toBeTruthy()
    expect(await screen.findByText(`detail for ${signIn}`)).toBeTruthy()
  })

  it('begins sign-in and shows the consent link in a new tab', async () => {
    const calls = stubServer()
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: /Open Google sign-in/ }))
    const link = await screen.findByRole('link', { name: /Open Google sign-in/ })
    expect(link.getAttribute('href')).toBe('https://accounts.google.com/o/oauth2/auth?x=1')
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')).toContain('noopener')
    expect(screen.getByText(/expires in 10 minutes/)).toBeTruthy()
    const begin = calls.find((c) => c.path.endsWith('/sign-in/begin'))
    expect(begin?.method).toBe('POST')
    expect(begin?.body).toEqual({})
  })

  it('finishes sign-in with the trimmed pasted address and shows Working', async () => {
    const calls = stubServer()
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: /Open Google sign-in/ }))
    const input = (await screen.findByRole('textbox', { name: 'Address from the browser bar' })) as HTMLInputElement
    expect(input.type).not.toBe('password')
    await userEvent.type(input, '  http://localhost:1/?code=abc&state=xyz  ')
    await userEvent.click(screen.getByRole('button', { name: /Finish sign-in/ }))
    await waitFor(() => expect(calls.some((c) => c.path.endsWith('/sign-in/complete'))).toBe(true))
    const complete = calls.find((c) => c.path.endsWith('/sign-in/complete'))
    expect(complete?.method).toBe('POST')
    expect(complete?.body).toEqual({ redirect_url: 'http://localhost:1/?code=abc&state=xyz' })
    expect(await screen.findByText(/Working — signed in/)).toBeTruthy()
    // The spent address and its link are cleared, not left to be pasted again.
    expect(screen.queryByRole('textbox', { name: 'Address from the browser bar' })).toBeNull()
    expect(screen.queryByRole('link', { name: /Open Google sign-in/ })).toBeNull()
  })

  it('shows the server refusal', async () => {
    stubServer({ complete: { status: 400, body: { error: 'Google signed in a different account' } } })
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: /Open Google sign-in/ }))
    await userEvent.type(
      await screen.findByRole('textbox', { name: 'Address from the browser bar' }),
      'http://localhost:1/?code=abc',
    )
    await userEvent.click(screen.getByRole('button', { name: /Finish sign-in/ }))
    expect(await screen.findByText('Google signed in a different account')).toBeTruthy()
  })

  it('shows a begin refusal', async () => {
    stubServer({ begin: { status: 400, body: { error: 'Another sign-in is still waiting' } } })
    renderPanel()
    await userEvent.click(await screen.findByRole('button', { name: /Open Google sign-in/ }))
    expect(await screen.findByText('Another sign-in is still waiting')).toBeTruthy()
  })

  it('offers no buttons outside operator mode', async () => {
    stubServer()
    renderPanel(false)
    expect(await screen.findByText('Turn on operator mode to sign in from here.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Open Google sign-in|Reconnect|Finish sign-in/ })).toBeNull()
  })

  it('asks for the account before offering sign-in', async () => {
    stubServer({ account: '' })
    renderPanel()
    expect(
      await screen.findByText("Set this connection's account (Edit details) before signing in."),
    ).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Open Google sign-in|Reconnect/ })).toBeNull()
  })

  it('words the start button as Reconnect when the sign-in expired', async () => {
    stubServer({ signIn: 'expired' })
    renderPanel()
    expect(await screen.findByRole('button', { name: /Reconnect/ })).toBeTruthy()
  })

  it.each([
    ['yes', 'Access: read only'],
    ['', 'Access: read only'],
    ['no', 'Access: read, draft and send'],
  ])('shows the access mode for read_only=%j', async (readOnly, label) => {
    stubServer({ readOnly })
    renderPanel()
    expect(await screen.findByText(label)).toBeTruthy()
  })

  it('warns about a blank client and still lets the operator sign in anyway', async () => {
    const warning = "Blank uses gog's built-in sign-in client, whose Google sign-ins expire after about 7 days."
    const calls = stubServer({ clientWarning: warning })
    const edit = vi.fn()
    renderPanel(true, edit)
    expect(await screen.findByText(warning)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Open Google sign-in$/ })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: /Edit details/ }))
    expect(edit).toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: /Sign in with the built-in client anyway/ }))
    await screen.findByRole('link', { name: /Open Google sign-in/ })
    const begin = calls.find((c) => c.path.endsWith('/sign-in/begin'))
    expect(begin?.body).toEqual({ accept_warnings: true })
  })
})
