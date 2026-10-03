import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { WorkflowsPage } from './workflows'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

it('lists templates and creates a workflow from the chosen one', async () => {
  localStorage.setItem('arcui_operator_mode', '1')
  const posts: Array<{ path: string; body: unknown }> = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: RequestInfo | URL, init?: RequestInit) => {
      const p = String(path)
      if (init?.method === 'POST') {
        posts.push({ path: p, body: JSON.parse(String(init.body)) })
        return new Response(JSON.stringify({ workflow_id: 'nightly' }))
      }
      if (p === '/api/team/roster') {
        return new Response(JSON.stringify({ agents: [{ agent_id: 'sales' }] }))
      }
      if (p === '/api/workflow-templates') {
        return new Response(
          JSON.stringify({
            templates: [{ id: 'digest', title: 'Daily digest', description: 'Summarise the day' }],
          }),
        )
      }
      return new Response(JSON.stringify({ workflows: [] }))
    }),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <WorkflowsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
  await userEvent.click(await screen.findByRole('button', { name: /new workflow/i }))
  const select = await screen.findByLabelText('Start from template')
  await screen.findByRole('option', { name: 'Daily digest' })
  await userEvent.selectOptions(select, 'digest')
  expect(screen.getByText('Summarise the day')).toBeTruthy()
  await userEvent.type(screen.getByPlaceholderText('onboarding'), 'nightly')
  // No owner chosen yet: the draft could never sign, so it cannot be created.
  expect(screen.getByRole('button', { name: 'Create draft' }).hasAttribute('disabled')).toBe(true)
  await screen.findByRole('option', { name: '@sales' })
  await userEvent.selectOptions(screen.getByLabelText('Owner agent'), '@sales')
  await userEvent.click(screen.getByRole('button', { name: 'Create draft' }))
  await waitFor(() => expect(posts).toHaveLength(1))
  expect(posts[0]).toEqual({
    path: '/api/workflows/from-template',
    body: { template: 'digest', workflow_id: 'nightly', owner: '@sales' },
  })
})

function LocationProbe() {
  const location = useLocation()
  return <div data-testid="where">{location.pathname + location.search}</div>
}

it('opens the run named by ?run= on its workflow detail page', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: RequestInfo | URL) => {
      const p = String(path)
      if (p === '/api/workflow-runs/run-42') {
        return new Response(
          JSON.stringify({ run_id: 'run-42', workflow_id: 'nightly', status: 'done', nodes: [], path_taken: [] }),
        )
      }
      return new Response(JSON.stringify({ workflows: [] }))
    }),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={['/workflows?run=run-42']}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/workflows" element={<WorkflowsPage />} />
          <Route path="/workflows/:id" element={<LocationProbe />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
  expect((await screen.findByTestId('where')).textContent).toBe('/workflows/nightly?run=run-42')
})
