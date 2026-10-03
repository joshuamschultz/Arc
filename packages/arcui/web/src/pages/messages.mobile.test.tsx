import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { MessagesPage } from '@/pages/messages'

vi.mock('@/hooks/use-chat', () => ({
  useChatSession: () => ({ messages: [], status: 'ready', send: vi.fn(), reset: vi.fn(), sessionKey: 's1' }),
}))
vi.mock('@/hooks/use-team-stream', () => ({ useTeamStream: () => ({ frames: [], status: 'ready' }) }))
vi.mock('@/components/shell/operator-avatar-menu', () => ({ OperatorAvatarMenu: () => null }))

Element.prototype.scrollIntoView = vi.fn()

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

function show() {
  vi.stubGlobal('fetch', vi.fn(async (request: RequestInfo | URL) => {
    const path = String(request)
    if (path.includes('/api/team/roster')) {
      return new Response(JSON.stringify({ agents: [{ agent_id: 'olivia', name: 'olivia', display_name: 'Olivia', online: true }] }))
    }
    return new Response(JSON.stringify({ channels: [], workflows: [], approvals: [] }))
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter><MessagesPage /></MemoryRouter>
    </QueryClientProvider>,
  )
}

it('shows the room list alone on a phone, then the conversation with a way back', async () => {
  const { container } = show()
  const grid = container.querySelector('aside')!.parentElement as HTMLElement
  expect(grid.className.split(' ')).not.toContain('grid-cols-[260px_1fr]')
  expect(grid.className).toContain('md:grid-cols-[260px_1fr]')
  const aside = container.querySelector('aside') as HTMLElement
  const main = container.querySelector('main') as HTMLElement
  expect(aside.className).not.toContain('max-md:hidden')
  expect(main.className).toContain('max-md:hidden')

  await userEvent.click(await screen.findByText('Olivia'))
  expect(aside.className).toContain('max-md:hidden')
  expect(main.className).not.toContain('max-md:hidden')

  await userEvent.click(screen.getByRole('button', { name: 'Back to rooms' }))
  expect(aside.className).not.toContain('max-md:hidden')
})

it('pads the composer by the iOS home-indicator inset', async () => {
  const { container } = show()
  await userEvent.click(await screen.findByText('Olivia'))
  const composer = container.querySelector('main [data-slot="composer"]') as HTMLElement
  expect(composer.className).toContain('env(safe-area-inset-bottom)')
})
