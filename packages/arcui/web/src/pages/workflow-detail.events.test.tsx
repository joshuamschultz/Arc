// SPEC-088 T-1377 (COMP-016, REQ-610): declared events on the workflow page.
//
// - `GET /api/workflows/{id}` lists `events: [{name, schema}]`; an Events tab
//   lists them (absent when the workflow declares none).
// - "Fire event" opens a dialog with a JSON payload textarea; Fire posts to
//   `POST /api/workflows/{id}/events/{name}` with a fresh `Idempotency-Key`
//   per submit.
// - 201 says a run started, 200 says the same key already started it, and a
//   422 lists each failing field.
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { WorkflowDetailPage } from './workflow-detail'

const WORKFLOW = {
  id: 'w1',
  name: 'Orders',
  status: 'signed',
  version: 1,
  nodes: [],
  edges: [],
  events: [
    { name: 'order_placed', schema: 'schemas/order.json' },
    { name: 'order_cancelled', schema: null },
  ],
}

interface Fire {
  path: string
  key: string | null
  body: unknown
}

function stubApi(workflow: unknown, replies: Array<{ status: number; body: unknown }>) {
  const fires: Fire[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: RequestInfo | URL, init?: RequestInit) => {
      const p = String(path)
      const reply = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
      if (init?.method === 'POST' && p.includes('/events/')) {
        const headers = new Headers(init.headers)
        fires.push({ path: p, key: headers.get('Idempotency-Key'), body: JSON.parse(String(init.body)) })
        const next = replies[Math.min(fires.length - 1, replies.length - 1)]
        return reply(next.body, next.status)
      }
      if (p === '/api/workflows/w1') return reply(workflow)
      return reply({ runs: [], agents: [], versions: [] })
    }),
  )
  return fires
}

function renderDetail() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={['/workflows/w1?tab=events']}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/workflows/:id" element={<WorkflowDetailPage />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function openFire(name: string) {
  const buttons = await screen.findAllByRole('button', { name: /fire event/i })
  const row = screen.getByText(name).closest('li')!
  const button = buttons.find((b) => row.contains(b))!
  await userEvent.click(button)
  return screen.findByLabelText(/event payload \(json\)/i)
}

it('lists declared events and fires one with a fresh idempotency key per submit', async () => {
  const fires = stubApi(WORKFLOW, [
    { status: 201, body: { run_id: 'evt-abc' } },
    { status: 200, body: { run_id: 'evt-abc' } },
  ])
  renderDetail()

  expect(await screen.findByText('order_placed')).toBeTruthy()
  expect(screen.getByText('order_cancelled')).toBeTruthy()
  expect(screen.getByText('schemas/order.json')).toBeTruthy()

  const payload = await openFire('order_placed')
  await userEvent.clear(payload)
  await userEvent.type(payload, '{{"order_id": "A-1"}')
  await userEvent.click(screen.getByRole('button', { name: /^fire$/i }))
  await waitFor(() => expect(fires).toHaveLength(1))
  expect(fires[0].path).toBe('/api/workflows/w1/events/order_placed')
  expect(fires[0].body).toEqual({ order_id: 'A-1' })
  expect(fires[0].key).toBeTruthy()
  expect(await screen.findByText(/started run evt-abc/i)).toBeTruthy()

  await userEvent.click(screen.getByRole('button', { name: /^fire$/i }))
  await waitFor(() => expect(fires).toHaveLength(2))
  expect(fires[1].key).toBeTruthy()
  expect(fires[1].key).not.toBe(fires[0].key)
  expect(await screen.findByText(/already started run evt-abc/i)).toBeTruthy()
})

it('shows each failing field of a 422', async () => {
  stubApi(WORKFLOW, [
    {
      status: 422,
      body: {
        errors: [
          { node_id: '', field: 'input.qty', error: 'is required' },
          { node_id: '', field: 'input.order_id', error: 'must be a string' },
        ],
      },
    },
  ])
  renderDetail()

  const payload = await openFire('order_placed')
  await userEvent.clear(payload)
  await userEvent.type(payload, '{{"order_id": 7}')
  await userEvent.click(screen.getByRole('button', { name: /^fire$/i }))
  expect(await screen.findByText(/input\.qty/)).toBeTruthy()
  expect(screen.getByText(/is required/)).toBeTruthy()
  expect(screen.getByText(/must be a string/)).toBeTruthy()
})

it('refuses a payload that is not a JSON object before sending', async () => {
  const fires = stubApi(WORKFLOW, [{ status: 201, body: { run_id: 'evt-x' } }])
  renderDetail()

  const payload = await openFire('order_placed')
  await userEvent.clear(payload)
  // `[[` types a literal bracket (userEvent key descriptor syntax).
  await userEvent.type(payload, '[[1, 2]')
  await userEvent.click(screen.getByRole('button', { name: /^fire$/i }))
  expect(await screen.findByText(/must be a json object/i)).toBeTruthy()
  expect(fires).toHaveLength(0)
})

it('has no Events tab when the workflow declares no events', async () => {
  stubApi({ ...WORKFLOW, events: [] }, [])
  render(
    <MemoryRouter initialEntries={['/workflows/w1']}>
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <Routes>
          <Route path="/workflows/:id" element={<WorkflowDetailPage />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
  expect(await screen.findByRole('tab', { name: /graph/i })).toBeTruthy()
  expect(screen.queryByRole('tab', { name: /events/i })).toBeNull()
})
