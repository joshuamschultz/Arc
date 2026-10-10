// SPEC-087 T-1288 (COMP-013 'My delegations', REQ-564, REQ-563): a signed-in
// person lists, creates and revokes their OWN delegations to an app.
//
// arcui has no profile page today, so the panel is a standalone component
// (`MyDelegationsPanel` from `@/components/my-delegations-panel`) that the
// green step mounts on the person's own surface. Route shapes are fixed by
// packages/arcui/tests/test_delegation_routes.py:
//   GET  /api/me/delegations            -> {delegations: [...]}
//   POST /api/me/delegations            {principal_did, scopes, ttl_seconds} -> 201 row
//   POST /api/me/delegations/{id}/revoke -> 200 row
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { MyDelegationsPanel } from '@/components/my-delegations-panel'

const APP = 'did:arc:default:app/billing'
const ROW = {
  id: 'd-1',
  person_did: 'did:arc:default:user/ada',
  principal_did: APP,
  scopes: ['gates:resolve'],
  created_at: '2026-10-10T08:00:00Z',
  expires_at: '2026-10-10T16:00:00Z',
  revoked: false,
}

interface Call {
  method: string
  path: string
  body?: Record<string, unknown>
}

function stubFetch(rows: unknown[] = [ROW]): Call[] {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const method = init?.method ?? 'GET'
      const body = init?.body ? JSON.parse(String(init.body)) : undefined
      calls.push({ method, path, body })
      const reply = (payload: unknown, status = 200) =>
        new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } })
      if (path === '/api/me/delegations' && method === 'GET') return reply({ delegations: rows })
      if (path === '/api/me/delegations' && method === 'POST') {
        return reply({ ...ROW, id: 'd-2', principal_did: body.principal_did, scopes: body.scopes }, 201)
      }
      if (path.endsWith('/revoke')) return reply({ ...ROW, revoked: true })
      return reply({})
    }),
  )
  return calls
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <MyDelegationsPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function rowFor(text: string): HTMLElement {
  const row = screen.getByText(text).closest('li, tr')
  if (!(row instanceof HTMLElement)) throw new Error(`no row for ${text}`)
  return row
}

const creates = (calls: Call[]) =>
  calls.filter((c) => c.method === 'POST' && c.path === '/api/me/delegations')

async function fillCreate(user: ReturnType<typeof userEvent.setup>, hours: string) {
  await user.type(screen.getByLabelText(/app did/i), APP)
  await user.click(screen.getByRole('checkbox', { name: /gates:resolve/i }))
  const ttl = screen.getByLabelText(/hours/i)
  await user.clear(ttl)
  await user.type(ttl, hours)
  await user.click(screen.getByRole('button', { name: /^(delegate|create|allow)/i }))
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('MyDelegationsPanel', () => {
  it('lists my delegations with the app DID and scopes', async () => {
    stubFetch()
    renderPanel()
    expect(await screen.findByRole('heading', { name: /my delegations/i })).toBeTruthy()
    const row = rowFor(APP)
    expect(row.textContent).toContain('gates:resolve')
  })

  it('offers only the decision scopes', async () => {
    stubFetch([])
    renderPanel()
    await screen.findByLabelText(/app did/i)
    expect(screen.getByRole('checkbox', { name: /gates:resolve/i })).toBeTruthy()
    expect(screen.getByRole('checkbox', { name: /tasks:write/i })).toBeTruthy()
    expect(screen.queryByRole('checkbox', { name: /inbox:send|runs:start|memory:write/i })).toBeNull()
    expect(screen.getAllByRole('checkbox')).toHaveLength(2)
  })

  it('creates a delegation with principal_did, scopes and ttl_seconds', async () => {
    const calls = stubFetch([])
    renderPanel()
    const user = userEvent.setup()
    await screen.findByLabelText(/app did/i)
    await fillCreate(user, '2')
    await waitFor(() => expect(creates(calls)).toHaveLength(1))
    expect(creates(calls)[0].body).toEqual({
      principal_did: APP,
      scopes: ['gates:resolve'],
      ttl_seconds: 7200,
    })
  })

  it('blocks a delegation longer than 8 hours before calling the server', async () => {
    const calls = stubFetch([])
    renderPanel()
    const user = userEvent.setup()
    await screen.findByLabelText(/app did/i)
    await fillCreate(user, '9')
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(creates(calls)).toHaveLength(0)
  })

  it('blocks a delegation with no scope', async () => {
    const calls = stubFetch([])
    renderPanel()
    const user = userEvent.setup()
    await screen.findByLabelText(/app did/i)
    await user.type(screen.getByLabelText(/app did/i), APP)
    await user.click(screen.getByRole('button', { name: /^(delegate|create|allow)/i }))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(creates(calls)).toHaveLength(0)
  })

  it('revokes only after confirmation, through the encoded revoke route', async () => {
    const calls = stubFetch()
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    renderPanel()
    const user = userEvent.setup()
    await screen.findByText(APP)
    const revoke = within(rowFor(APP)).getByRole('button', { name: /revoke/i })

    await user.click(revoke)
    expect(calls.some((c) => c.path.endsWith('/revoke'))).toBe(false)

    await user.click(revoke)
    expect(confirm).toHaveBeenCalledTimes(2)
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'POST' && c.path === '/api/me/delegations/d-1/revoke')).toBe(true),
    )
  })

  it('offers no revoke on a revoked delegation', async () => {
    stubFetch([{ ...ROW, revoked: true }])
    renderPanel()
    await screen.findByText(APP)
    expect(within(rowFor(APP)).queryByRole('button', { name: /revoke/i })).toBeNull()
  })
})
