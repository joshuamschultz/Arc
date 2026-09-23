import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { focusManager, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { TasksPage } from '@/pages/tasks'

afterEach(() => {
  cleanup()
  focusManager.setFocused(undefined)
  vi.unstubAllGlobals()
})

it('shows one current page and refreshes it after a task changes status', async () => {
  let secondIsActive = true
  const task = (id: string, title: string) => ({
    id, title, status: 'todo', priority: 'medium', updated_at: '2099-01-01T00:00:00Z',
  })
  vi.stubGlobal('fetch', vi.fn(async (request: RequestInfo | URL) => {
    const path = String(request)
    if (path.includes('/api/team/roster')) return new Response(JSON.stringify({ agents: [] }))
    if (path.includes('cursor=cursor-2')) {
      return new Response(JSON.stringify({
        tasks: secondIsActive ? [task('second', 'Second active')] : [],
        next_cursor: 'cursor-history',
      }))
    }
    return new Response(JSON.stringify({
      tasks: [task('head', 'Head active')], next_cursor: 'cursor-2',
    }))
  }))
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: 0, retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/tasks']}><TasksPage /></MemoryRouter>
    </QueryClientProvider>,
  )
  await screen.findByRole('button', { name: /Head active/ })
  await userEvent.click(screen.getByRole('button', { name: 'Help for Tasks' }))
  expect(await screen.findByText(/Track work across the fleet/)).toBeTruthy()
  await userEvent.keyboard('{Escape}')
  await userEvent.click(screen.getByRole('button', { name: 'Load more tasks' }))
  await screen.findByRole('button', { name: /Second active/ })
  secondIsActive = false
  focusManager.setFocused(false)
  focusManager.setFocused(true)
  await waitFor(() => expect(screen.queryByRole('button', { name: /Second active/ })).toBeNull())
  expect(screen.queryByRole('button', { name: /Head active/ })).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: 'Previous page' }))
  expect(await screen.findByRole('button', { name: /Head active/ })).toBeTruthy()
  client.clear()
})

it('shows global counts and finds a historical match beyond the first page', async () => {
  const stamp = '2099-01-01T00:00:00Z'
  const facets = {
    statuses: { todo: 1, done: 1101 }, priorities: { medium: 1102 },
    owners: { 'did:arc:rare': 1 }, tags: { rare: 1 }, total: 1102,
    blocked: 0, done_today: 1, avg_done_seconds: null,
  }
  const requests: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (request: RequestInfo | URL) => {
    const path = String(request)
    requests.push(path)
    if (path.includes('/api/team/roster')) return new Response(JSON.stringify({ agents: [] }))
    if (path.includes('status=done')) return new Response(JSON.stringify({
      tasks: [{ id: 'old', title: 'Rare historical match', status: 'done', priority: 'medium',
        owner_did: 'did:arc:rare', tags: ['rare'], updated_at: stamp }], facets,
      projections: { old: { blocked: false, dependencies: {}, dependency_total: 0,
        children: [], child_total: 0, child_done: 0 } },
    }))
    return new Response(JSON.stringify({
      tasks: [{ id: 'parent', title: 'Visible parent', status: 'todo', priority: 'medium',
        blocked_by: ['old'], updated_at: stamp }], facets,
      projections: { parent: { blocked: false,
        dependencies: { old: { id: 'old', title: 'Rare historical match', status: 'done' } },
        dependency_total: 1, children: [], child_total: 0, child_done: 0 } },
      next_cursor: 'older',
    }))
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}>
    <MemoryRouter><TasksPage /></MemoryRouter>
  </QueryClientProvider>)
  const parent = await screen.findByRole('button', { name: /Visible parent/ })
  expect(screen.getByText(/1102 total/)).toBeTruthy()
  expect(parent.textContent).not.toContain('blocked')
  await userEvent.click(parent)
  expect(await screen.findByText('Rare historical match')).toBeTruthy()
  await userEvent.keyboard('{Escape}')
  await userEvent.click(screen.getByRole('button', { name: 'done1101' }))
  expect(await screen.findByRole('button', { name: /Rare historical match/ })).toBeTruthy()
  expect(requests.some((path) => path.includes('status=done'))).toBe(true)
  client.clear()
})

it('creates an unassigned task while roster loading is stalled', async () => {
  let created = false
  vi.stubGlobal('fetch', vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
    const path = String(request)
    if (path.includes('/api/team/roster')) return new Promise<Response>(() => {})
    if (path.includes('/api/team/tasks') && init?.method === 'POST') {
      created = true
      return new Response(JSON.stringify({ id: 'new', title: 'New backlog', status: 'backlog',
        owner_notification: 'not_applicable' }), { status: 201 })
    }
    return new Response(JSON.stringify({ tasks: created ? [
      { id: 'new', title: 'New backlog', status: 'backlog', priority: 'medium' }] : [],
      facets: { statuses: { backlog: created ? 1 : 0 }, priorities: { medium: created ? 1 : 0 },
        owners: {}, tags: {}, total: created ? 1 : 0, blocked: 0, done_today: 0,
        avg_done_seconds: null }, projections: {} }))
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}>
    <MemoryRouter><TasksPage /></MemoryRouter>
  </QueryClientProvider>)
  await userEvent.click(screen.getByRole('button', { name: 'Operator controls off' }))
  await userEvent.click(screen.getByRole('button', { name: 'New task' }))
  await userEvent.type(screen.getByLabelText('Title'), 'New backlog')
  await userEvent.click(screen.getByRole('button', { name: 'Create task' }))
  expect(await screen.findByRole('status')).toHaveProperty('textContent', 'Task created in the fleet backlog.')
  expect(created).toBe(true)
  client.clear()
})
