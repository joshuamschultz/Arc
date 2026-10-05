import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SyncWorkerStatusLine } from '@/components/sync-worker-status'

function show(body: unknown) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL) => {
      expect(String(request)).toContain('/api/knowledge/sync-worker')
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <SyncWorkerStatusLine />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('SyncWorkerStatusLine', () => {
  it.each([
    ['up', 'Sync running'],
    ['restarting', 'Sync paused: restarting'],
    ['down', 'Sync stopped'],
  ])('shows the worker %s', async (state, title) => {
    show({ state, pid: 1, restarts: 0, detail: state === 'up' ? '' : 'exited with code 1' })
    const line = await screen.findByRole('status', { name: 'Sync worker' })
    expect(line.textContent).toContain(title)
    expect(line.getAttribute('data-state')).toBe(state)
  })

  it('says search keeps working while the worker restarts', async () => {
    show({ state: 'restarting', pid: null, restarts: 2, detail: 'hung (missed heartbeats); killed' })
    const line = await screen.findByRole('status', { name: 'Sync worker' })
    expect(line.textContent).toContain('Search keeps working')
    expect(line.textContent).toContain('hung (missed heartbeats); killed')
  })

  it('renders nothing for an answer it does not understand', async () => {
    show({ items: [] })
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(screen.queryByRole('status', { name: 'Sync worker' })).toBeNull()
  })
})
