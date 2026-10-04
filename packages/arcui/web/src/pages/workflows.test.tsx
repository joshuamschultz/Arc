import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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

it('opens the run named by ?run= on its own run page', async () => {
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
          <Route path="/workflows/:id/runs/:runId" element={<LocationProbe />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
  expect((await screen.findByTestId('where')).textContent).toBe('/workflows/nightly/runs/run-42')
})

it('shows an unreadable workflow with a red badge and the fix, not an empty list', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            workflows: [
              {
                id: 'morning-briefing',
                name: 'morning-briefing',
                version: 0,
                status: 'unreadable',
                health: 'unreadable',
                health_detail: 'cannot be read (join: not allowed)',
                health_fix_action: 'migrate',
              },
              {
                id: 'seo',
                name: 'seo',
                version: 2,
                status: 'draft',
                health: 'needs_resign',
                health_detail: 'its signature no longer matches',
                health_fix_action: 'migrate',
              },
            ],
          }),
        ),
    ),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <WorkflowsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
  expect(await screen.findByText('Unreadable')).toBeTruthy()
  expect(screen.getByText(/join: not allowed/)).toBeTruthy()
  expect(screen.getAllByRole('button', { name: 'Preview fix' })).toHaveLength(2)
  expect(screen.getByText('Needs re-sign')).toBeTruthy()
  expect(screen.queryByText(/arc workflow/)).toBeNull()
  expect(screen.queryByText('No workflows yet')).toBeNull()
})

it('previews, then migrates and re-signs an unreadable workflow with buttons only', async () => {
  const calls: { url: string; body: unknown }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        const body = JSON.parse(String(init.body))
        calls.push({ url, body })
        return new Response(
          JSON.stringify({
            workflow_id: 'morning-briefing',
            action: body.apply ? 'rewritten' : 'would_rewrite',
            files: ['workflow.toml'],
            nodes: ['a'],
            reason: '',
          }),
        )
      }
      return new Response(
        JSON.stringify({
          workflows: [
            {
              id: 'morning-briefing',
              name: 'morning-briefing',
              version: 0,
              status: 'unreadable',
              health: 'unreadable',
              health_detail: 'cannot be read (join: not allowed)',
              health_fix_action: 'migrate',
            },
          ],
        }),
      )
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
  fireEvent.click(await screen.findByRole('button', { name: 'Preview fix' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Migrate and re-sign' }))
  await waitFor(() => expect(calls).toHaveLength(2))
  expect(calls[0].body).toEqual({ apply: false, resign: false })
  expect(calls[1].body).toEqual({ apply: true, resign: true })
  expect(calls[1].url).toContain('/api/workflows/morning-briefing/migrate')
})
