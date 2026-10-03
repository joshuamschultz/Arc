import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { UsersPanel } from '@/pages/users'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const USER = {
  id: 'u1',
  email: 'ada@example.com',
  did: 'did:arc:1',
  handle: 'ada',
  display_name: 'Ada',
  roles: ['operator'],
  pairings: [],
  disabled: false,
  created_at: '2026-01-01T00:00:00Z',
}

interface Call {
  method: string
  path: string
  body?: Record<string, unknown>
}

function stubFetch(listStatus = 200): Call[] {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const method = init?.method ?? 'GET'
      const reply = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
      calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      if (path === '/api/users' && method === 'GET') {
        return listStatus === 200
          ? reply({ users: [USER] })
          : reply({ error: 'operator role required' }, listStatus)
      }
      if (path === '/api/users/invites') {
        return reply(
          { link_path: '/#invite=tok9', expires_at: 'soon', email: 'new@example.com', role: 'viewer' },
          201,
        )
      }
      if (path.endsWith('/disable')) return reply({ user: { ...USER, disabled: true } })
      return reply({})
    }),
  )
  return calls
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <UsersPanel />
    </QueryClientProvider>,
  )
}

describe('UsersPanel', () => {
  it('lists people with their state', async () => {
    stubFetch()
    renderPanel()
    expect(await screen.findByText('ada@example.com')).toBeTruthy()
    expect(screen.getByText('Active')).toBeTruthy()
  })

  it('creates an invite and shows a one-time link', async () => {
    const calls = stubFetch()
    renderPanel()
    const user = userEvent.setup()
    await screen.findByText('ada@example.com')
    await user.click(screen.getByRole('button', { name: /Invite/ }))
    await user.type(screen.getByLabelText('Invite email'), 'new@example.com')
    await user.click(screen.getByRole('button', { name: 'Create invite link' }))

    await screen.findByText(`${window.location.origin}/#invite=tok9`)
    expect(screen.getByText(/This link works once/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /Copy link/ })).toBeTruthy()
    const post = calls.find((c) => c.path === '/api/users/invites')
    expect(post?.body).toEqual({ email: 'new@example.com', role: 'viewer' })
  })

  it('disables a person through the URL-encoded email path', async () => {
    const calls = stubFetch()
    renderPanel()
    await screen.findByText('ada@example.com')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Disable' }))
    expect(calls.some((c) => c.method === 'POST' && c.path === '/api/users/ada%40example.com/disable')).toBe(true)
  })

  it('tells a viewer that only an operator can manage people', async () => {
    stubFetch(403)
    renderPanel()
    expect(await screen.findByText('Only an operator can manage people.')).toBeTruthy()
  })
})
