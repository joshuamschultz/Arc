// SPEC-088 T-1377 (COMP-018, REQ-625): Settings -> Apps -> Callback origins.
//
// Route shapes: packages/arcui/src/arcui/routes/work_event_callbacks.py.
// - GET  /api/work-events/callbacks            -> {callbacks: [entry]}
// - POST /api/work-events/callbacks            {origin, allow_private, clearance}
//        -> 201 {callback, status, approval_id?}; `pending` above personal tier
//           with the approval row that activates it.
// - POST /api/work-events/callbacks/{id}/remove -> {callback}
// A removed entry is history: shown without a Remove button.
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { AppsPanel } from '@/pages/apps'

const ENTRY = {
  id: 'cb-1',
  origin: 'https://hooks.example.com',
  allow_private: false,
  clearance: 'UNCLASSIFIED',
  status: 'active',
  created_by: 'did:arc:default:user/op',
  approved_by: null,
  created_at: '2026-10-10T10:00:00+00:00',
}

const REMOVED = { ...ENTRY, id: 'cb-0', origin: 'https://old.example.com', status: 'removed' }

interface Call {
  method: string
  path: string
  body?: Record<string, unknown>
}

function stubFetch(addReply: { status: number; body: unknown }): Call[] {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const method = init?.method ?? 'GET'
      calls.push({ method, path, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      const reply = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
      if (path === '/api/principals') return reply({ principals: [] })
      if (path === '/api/work-events/callbacks' && method === 'GET') {
        return reply({ callbacks: [REMOVED, ENTRY] })
      }
      if (path === '/api/work-events/callbacks' && method === 'POST') {
        return reply(addReply.body, addReply.status)
      }
      if (path.endsWith('/remove')) return reply({ callback: { ...ENTRY, status: 'removed' } })
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
        <AppsPanel />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

async function panel(): Promise<HTMLElement> {
  const heading = await screen.findByRole('heading', { name: /callback origins/i })
  return heading.closest('section') as HTMLElement
}

it('lists callback origins; a removed one has no Remove button', async () => {
  stubFetch({ status: 201, body: {} })
  renderPanel()
  const section = await panel()

  const active = (await within(section).findByText('https://hooks.example.com')).closest('li')!
  expect(within(active).getByText('active')).toBeTruthy()
  expect(within(active).getByRole('button', { name: /remove/i })).toBeTruthy()
  const removed = within(section).getByText('https://old.example.com').closest('li')!
  expect(within(removed).queryByRole('button', { name: /remove/i })).toBeNull()
})

it('adds an origin with allow-private and a clearance (active at personal)', async () => {
  const calls = stubFetch({
    status: 201,
    body: { callback: { ...ENTRY, id: 'cb-2', origin: 'https://new.example.com' }, status: 'active' },
  })
  renderPanel()
  const section = await panel()

  await userEvent.type(within(section).getByLabelText(/^origin/i), 'https://new.example.com')
  await userEvent.click(within(section).getByRole('checkbox', { name: /allow private/i }))
  await userEvent.selectOptions(within(section).getByRole('combobox', { name: /clearance/i }), 'CUI')
  await userEvent.click(within(section).getByRole('button', { name: /add origin/i }))

  await waitFor(() =>
    expect(calls.find((c) => c.method === 'POST')).toEqual({
      method: 'POST',
      path: '/api/work-events/callbacks',
      body: { origin: 'https://new.example.com', allow_private: true, clearance: 'CUI' },
    }),
  )
  // The list is re-read after the add.
  await waitFor(() =>
    expect(calls.filter((c) => c.method === 'GET' && c.path === '/api/work-events/callbacks').length).toBe(2),
  )
})

it('a pending add at enterprise links to the approval card', async () => {
  stubFetch({
    status: 201,
    body: {
      callback: { ...ENTRY, id: 'cb-3', origin: 'https://pending.example.com', status: 'pending' },
      status: 'pending',
      approval_id: 'cbapprove_42',
    },
  })
  renderPanel()
  const section = await panel()

  await userEvent.type(within(section).getByLabelText(/^origin/i), 'https://pending.example.com')
  await userEvent.click(within(section).getByRole('button', { name: /add origin/i }))

  const link = await within(section).findByRole('link', { name: /open the approval/i })
  expect(link.getAttribute('href')).toBe('/approvals#cbapprove_42')
})

it('shows the refusal when an add is refused', async () => {
  stubFetch({ status: 409, body: { error: 'duplicate_origin' } })
  renderPanel()
  const section = await panel()

  await userEvent.type(within(section).getByLabelText(/^origin/i), 'https://hooks.example.com')
  await userEvent.click(within(section).getByRole('button', { name: /add origin/i }))
  expect(await within(section).findByRole('alert')).toBeTruthy()
  expect(within(section).getByRole('alert').textContent).toMatch(/already/i)
})

it('removes an origin after a confirm', async () => {
  const calls = stubFetch({ status: 201, body: {} })
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  renderPanel()
  const section = await panel()

  const row = (await within(section).findByText('https://hooks.example.com')).closest('li')!
  await userEvent.click(within(row).getByRole('button', { name: /remove/i }))
  await waitFor(() =>
    expect(calls.some((c) => c.method === 'POST' && c.path === '/api/work-events/callbacks/cb-1/remove')).toBe(true),
  )
})
