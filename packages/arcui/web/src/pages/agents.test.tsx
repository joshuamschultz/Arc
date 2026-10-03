import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { AgentsPage } from '@/pages/agents'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

interface Post {
  path: string
  body: Record<string, unknown>
}

function stubFetch(): Post[] {
  const posts: Post[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const reply = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
      if ((init?.method ?? 'GET') === 'POST') {
        posts.push({ path, body: JSON.parse(String(init?.body ?? '{}')) })
        return reply(
          { agent_id: 'helper', name: 'helper', did: 'did:arc:h', team_registered: true, notice: null },
          201,
        )
      }
      if (path.includes('/api/team/roster')) return reply({ agents: [] })
      return reply({})
    }),
  )
  return posts
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter initialEntries={['/agents']}>
          <Routes>
            <Route path="/agents" element={<AgentsPage />} />
            <Route path="/agents/:id" element={<p>agent page</p>} />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  )
}

describe('AgentsPage new agent', () => {
  it('shows plain words in the empty state, with no command', async () => {
    stubFetch()
    renderPage()
    await screen.findByText('No agents yet')
    expect(document.body.textContent).not.toContain('arc team register')
    expect(screen.getAllByRole('button', { name: /New agent/ }).length).toBeGreaterThan(1)
  })

  it('creates an agent through the dialog and opens it', async () => {
    const posts = stubFetch()
    renderPage()
    const user = userEvent.setup()
    await screen.findByText('No agents yet')
    await user.click(screen.getAllByRole('button', { name: /New agent/ })[0])
    await user.type(await screen.findByLabelText('Name'), 'helper')
    await user.click(screen.getByRole('button', { name: 'Create agent' }))

    await screen.findByText('agent page')
    expect(posts[0].path).toBe('/api/agents')
    expect(posts[0].body).toMatchObject({
      name: 'helper',
      model: 'anthropic/claude-sonnet-4-5-20250929',
      tier: 'personal',
    })
  })

  it('imports an agent from its identity file', async () => {
    const posts = stubFetch()
    renderPage()
    const user = userEvent.setup()
    await screen.findByText('No agents yet')
    await user.click(screen.getAllByRole('button', { name: /New agent/ })[0])
    await user.click(await screen.findByRole('button', { name: 'Import' }))
    await user.type(screen.getByLabelText('Name'), 'helper')
    const file = new File(['# Who I am'], 'identity.md', { type: 'text/markdown' })
    Object.defineProperty(file, 'text', { value: async () => '# Who I am' })
    await user.upload(screen.getByLabelText(/identity\.md/), file)
    await user.click(screen.getByRole('button', { name: 'Import agent' }))

    await screen.findByText('agent page')
    expect(posts[0].path).toBe('/api/agents/import')
    expect(posts[0].body.files).toEqual({ 'identity.md': '# Who I am' })
  })

  it('names the rule when the agent name is not allowed', async () => {
    stubFetch()
    renderPage()
    const user = userEvent.setup()
    await screen.findByText('No agents yet')
    await user.click(screen.getAllByRole('button', { name: /New agent/ })[0])
    await user.type(await screen.findByLabelText('Name'), 'Bad Name')
    expect(screen.getByText(/lowercase letters, digits/i)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Create agent' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
