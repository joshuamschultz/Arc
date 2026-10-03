import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { AgentReachCard, TeamChatNotice } from '@/pages/agent-detail'

afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.unstubAllGlobals()
})

function wrap(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AgentReachCard', () => {
  it('links a needs-attention account to its card on Connections', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              instances: [
                {
                  instance: 'work gmail',
                  extension_display_name: 'Gmail',
                  approval: 'auto',
                  needs_attention: true,
                },
              ],
              extensions_roots: [],
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
      ),
    )
    wrap(<AgentReachCard agentId="olivia" />)
    const link = await screen.findByRole('link', { name: /Needs attention: reconnect/ })
    expect(link.getAttribute('href')).toBe('/connections?connection=work%20gmail')
  })
})

describe('TeamChatNotice', () => {
  it('adds the agent to team chat for an operator', async () => {
    localStorage.setItem('arcui_operator_mode', '1')
    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify({ agent_id: 'olivia', did: 'd', team_registered: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
    )
    vi.stubGlobal('fetch', fetchMock)
    wrap(<TeamChatNotice agentId="olivia" />)
    expect(screen.getByText('Not in team chat yet')).toBeTruthy()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Add to team' }))
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(path).toBe('/api/agents/olivia/register')
    expect(init.method).toBe('POST')
  })
})
