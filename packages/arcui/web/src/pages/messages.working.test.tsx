import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { MessagesPage } from '@/pages/messages'

const chat = vi.hoisted(() => ({ working: true }))

vi.mock('@/hooks/use-chat', () => ({
  useChatSession: () => ({
    messages: [
      { id: 'h0', role: 'agent', text: 'old answer from August', time: '2026-08-27T10:00:00Z' },
      { id: 'h1', role: 'user', text: 'my message sent while away', time: '2026-10-04T10:00:00Z' },
    ],
    status: 'ready',
    sessionKey: 's1',
    working: chat.working,
    sendMessage: vi.fn(),
    resetForNewSession: vi.fn(),
  }),
}))
vi.mock('@/hooks/use-team-stream', () => ({ useTeamStream: () => ({ frames: [], status: 'ready' }) }))
vi.mock('@/components/shell/operator-avatar-menu', () => ({ OperatorAvatarMenu: () => null }))

Element.prototype.scrollIntoView = vi.fn()

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

async function openChat() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL) => {
      if (String(request).includes('/api/team/roster')) {
        return new Response(
          JSON.stringify({
            agents: [{ agent_id: 'mc', name: 'mc', display_name: 'MC', online: true }],
          }),
        )
      }
      return new Response(JSON.stringify({ channels: [], workflows: [], approvals: [] }))
    }),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <MessagesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await userEvent.click((await screen.findAllByText('MC'))[0])
}

it('returning mid-run shows the pending user message and a working indicator', async () => {
  chat.working = true
  await openChat()
  expect(await screen.findByText('my message sent while away')).toBeTruthy()
  expect(screen.getByTestId('chat-working').textContent).toContain('MC is working')
})

it('shows no working indicator when no run is in flight', async () => {
  chat.working = false
  await openChat()
  await screen.findByText('my message sent while away')
  expect(screen.queryByTestId('chat-working')).toBeNull()
})
