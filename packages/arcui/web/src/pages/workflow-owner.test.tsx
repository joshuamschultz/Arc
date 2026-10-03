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

const ROSTER = {
  agents: [
    { agent_id: 'sales', name: 'sales' },
    { agent_id: 'support', name: 'support' },
  ],
}

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
      if (p === '/api/team/roster') return new Response(JSON.stringify(ROSTER))
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

const DRAFT = { id: 'w1', name: 'W1', status: 'draft', version: 2, nodes: [], edges: [] }

it('lets a draft with the placeholder owner be given a real owner agent', async () => {
  const patches: Array<[string, unknown]> = []
  renderDetail({ ...DRAFT, owner: '@operator' }, patches)

  const select = await screen.findByLabelText('Owner agent')
  await screen.findByRole('option', { name: '@support' })
  await userEvent.selectOptions(select, '@support')

  await waitFor(() =>
    expect(patches).toEqual([['/api/workflows/w1', { owner: '@support', expected_version: 2 }]]),
  )
})

it('blocks adding a node until the workflow has a real owner', async () => {
  renderDetail({ ...DRAFT, owner: '@operator' }, [])

  const add = await screen.findByRole('button', { name: /add node/i })
  expect(add.hasAttribute('disabled')).toBe(true)
  expect(screen.getByText(/choose an owner agent first/i)).toBeTruthy()
})

it('allows adding a node once the owner is a real agent', async () => {
  renderDetail({ ...DRAFT, owner: '@sales' }, [])

  const add = await screen.findByRole('button', { name: /add node/i })
  expect(add.hasAttribute('disabled')).toBe(false)
})
