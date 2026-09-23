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

it('keeps the head live and refreshes an active task after browsing page two', async () => {
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
  expect(screen.getByRole('button', { name: /Head active/ })).toBeTruthy()
  client.clear()
})
