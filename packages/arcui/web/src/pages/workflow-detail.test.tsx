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
