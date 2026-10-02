import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { WorkflowDetailPage } from './workflow-detail'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function renderDetail(workflow: Record<string, unknown>, patches: Array<[string, unknown]>) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: RequestInfo | URL, init?: RequestInit) => {
      const p = String(path)
      if (init?.method === 'PATCH') {
        patches.push([p, JSON.parse(String(init.body))])
        return new Response(JSON.stringify({}))
      }
      if (p === '/api/workflows/w1') return new Response(JSON.stringify(workflow))
      return new Response(JSON.stringify({ runs: [], agents: [], versions: [] }))
    }),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={['/workflows/w1']}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/workflows/:id" element={<WorkflowDetailPage />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const BASE = { id: 'w1', name: 'W1', status: 'signed', version: 1, nodes: [], edges: [] }

it('shows paused-by-breaker and re-enable', async () => {
  const patches: Array<[string, unknown]> = []
  renderDetail(
    {
      ...BASE,
      schedule: {
        agent_id: 'sales',
        schedule_id: 'wf:w1',
        enabled: false,
        disabled_reason: 'breaker',
        disabled_at: '2026-10-01T22:00:05+00:00',
        last_error: 'start refused',
      },
    },
    patches,
  )
  expect(await screen.findByText(/paused by safety breaker/i)).toBeTruthy()
  await userEvent.click(screen.getByRole('button', { name: /re-enable/i }))
  await waitFor(() =>
    expect(patches).toEqual([['/api/agents/sales/schedules/wf%3Aw1', { enabled: true }]]),
  )
})

it('shows next fire for an enabled schedule and no re-enable button', async () => {
  renderDetail(
    {
      ...BASE,
      schedule: {
        agent_id: 'sales',
        schedule_id: 'wf:w1',
        enabled: true,
        next_fire_at: '2026-10-02T22:00:00+00:00',
      },
    },
    [],
  )
  expect(await screen.findByText(/scheduled · next/i)).toBeTruthy()
  expect(screen.queryByRole('button', { name: /re-enable/i })).toBeNull()
})

it('shows disabled by operator with a re-enable button', async () => {
  renderDetail(
    {
      ...BASE,
      schedule: {
        agent_id: 'sales',
        schedule_id: 'wf:w1',
        enabled: false,
        disabled_reason: 'operator',
      },
    },
    [],
  )
  expect(await screen.findByText(/disabled by operator/i)).toBeTruthy()
  expect(screen.getByRole('button', { name: /re-enable/i })).toBeTruthy()
})

it('Test run posts to the test-run route', async () => {
  const posts: string[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: RequestInfo | URL, init?: RequestInit) => {
      const p = String(path)
      if (init?.method === 'POST') {
        posts.push(p)
        return new Response(JSON.stringify({ run_id: 'run-9' }))
      }
      if (p === '/api/workflows/w1') {
        return new Response(
          JSON.stringify({ id: 'w1', name: 'W1', status: 'draft', version: 1, nodes: [], edges: [] }),
        )
      }
      return new Response(JSON.stringify({ runs: [], agents: [], versions: [] }))
    }),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={['/workflows/w1']}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/workflows/:id" element={<WorkflowDetailPage />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
  await userEvent.click(await screen.findByRole('button', { name: /test run/i }))
  await waitFor(() => expect(posts).toEqual(['/api/workflows/w1/test-run']))
})
