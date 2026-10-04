import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { OverviewTab } from '@/pages/agent-detail'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function show(contextWindow: unknown) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL) => {
      const path = String(request)
      let body: unknown = {}
      if (path.endsWith('/context-window')) body = contextWindow
      else if (path.endsWith('/api/agents/mc')) body = { agent_id: 'mc', name: 'mc', online: true }
      else if (path.includes('/config')) {
        body = { config: { context: { max_tokens: 200000 }, llm: { model: 'claude' } } }
      } else if (path.includes('/sessions')) body = { sessions: [] }
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <OverviewTab agentId="mc" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const PROMPT = {
  trace_id: 't1',
  timestamp: '2026-10-04T10:00:00+00:00',
  model: 'claude',
  input_tokens: 162000,
  cache_read_tokens: 150000,
  cache_write_tokens: 11000,
  output_tokens: 40,
}

describe('Context Window card', () => {
  it('shows the chat prompt size, cache numbers, session and send time', async () => {
    show({ session_id: 'cb21ee7c23023125', turn_in_flight: false, prompt: PROMPT })
    expect(await screen.findByText('81%')).toBeTruthy()
    expect(screen.getAllByText('162,000').length).toBeGreaterThan(0)
    expect(screen.getByText('150,000')).toBeTruthy()
    expect(screen.getByText('11,000')).toBeTruthy()
    const label = screen.getByTestId('ctx-label').textContent ?? ''
    expect(label).toContain('cb21ee7c')
    expect(label).toContain('sent')
    expect(screen.queryByTestId('ctx-turn-running')).toBeNull()
  })

  it('says a turn is running while still showing the last completed prompt', async () => {
    show({ session_id: 'cb21ee7c23023125', turn_in_flight: true, prompt: PROMPT })
    expect(await screen.findByTestId('ctx-turn-running')).toBeTruthy()
    expect(screen.getAllByText('162,000').length).toBeGreaterThan(0)
  })

  it('says there is no chat prompt yet instead of showing another call', async () => {
    show({ session_id: null, turn_in_flight: false, prompt: null })
    expect((await screen.findByTestId('ctx-label')).textContent).toBe('no chat prompt yet')
  })
})
