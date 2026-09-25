import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { QueuePage } from './queue'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function showQueue() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<MemoryRouter><QueryClientProvider client={client}>
    <QueuePage />
  </QueryClientProvider></MemoryRouter>)
}

const control = {
  revision: 4,
  paused: false,
  limits: { max_concurrent: 2, max_queued: 10, wait_timeout: 60, history_limit: 1000 },
}
const job = {
  call_id: 'call-1', tenant_id: 'tenant-a', owner_id: 'owner-1', agent_id: 'agent-1',
  session_id: null, run_id: null, state: 'running', version: 2,
  created_at: 1, updated_at: 1, provider_scope: null, attempt_id: null,
}

it('shows a requested cancellation as pending, not confirmed', async () => {
  const fetchMock = vi.fn(async (path: RequestInfo | URL) => {
    if (String(path) === '/api/queue/control') return new Response(JSON.stringify(control))
    if (String(path).startsWith('/api/queue/jobs')) {
      return new Response(JSON.stringify({ jobs: [job], next_cursor: null }))
    }
    return new Response(JSON.stringify({ status: 'requested', job: { ...job, state: 'cancel_requested' } }))
  })
  vi.stubGlobal('fetch', fetchMock)
  showQueue()
  expect(await screen.findByText('call-1')).toBeTruthy()
  await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(await screen.findByRole('status')).toHaveProperty(
    'textContent', 'Cancellation requested; the provider may still finish',
  )
})

it('retains the original revision after a conflicting limits update', async () => {
  const writes: Array<Record<string, unknown>> = []
  const fetchMock = vi.fn(async (path: RequestInfo | URL, options?: RequestInit) => {
    if (String(path) === '/api/queue/control') return new Response(JSON.stringify(control))
    if (String(path).startsWith('/api/queue/jobs')) {
      return new Response(JSON.stringify({ jobs: [], next_cursor: null }))
    }
    writes.push(JSON.parse(String(options?.body)) as Record<string, unknown>)
    return new Response(JSON.stringify({ error: 'stale_queue_revision' }), { status: 409 })
  })
  vi.stubGlobal('fetch', fetchMock)
  showQueue()
  const maxConcurrent = await screen.findByLabelText('Concurrent calls')
  await userEvent.clear(maxConcurrent)
  await userEvent.type(maxConcurrent, '3')
  await userEvent.click(screen.getByRole('button', { name: 'Save limits' }))
  await waitFor(() => expect(writes).toHaveLength(1))
  expect(writes[0].expected_revision).toBe(4)
  expect(await screen.findByText(/Reload the current values/)).toBeTruthy()
  expect(screen.getByRole('button', { name: 'Reload limits' })).toBeTruthy()
})
