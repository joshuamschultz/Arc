import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AgentCard } from './agent-card'
import type { Agent } from '@/lib/types'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const agent = { agent_id: 'alpha', name: 'alpha', online: true } as Agent

function showCard(health: { rejected: unknown[] }) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: RequestInfo | URL) => {
      if (String(path).includes('/prompts/health')) return new Response(JSON.stringify(health))
      return new Response(JSON.stringify({ buckets: [] }))
    }),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <AgentCard agent={agent} onOpen={() => {}} />
    </QueryClientProvider>,
  )
}

it('shows a prompt rejected badge when the agent refuses its prompts', async () => {
  showCard({
    rejected: [{ package: 'workspace', name: 'identity', reason: 'identity.md was edited after it was signed.' }],
  })
  const badge = await screen.findByRole('alert')
  expect(badge.textContent).toBe('Prompt rejected')
  expect(badge.getAttribute('title')).toContain('identity.md was edited after it was signed.')
})

it('shows no badge when every prompt verifies', async () => {
  showCard({ rejected: [] })
  await screen.findByText(/calls today/)
  expect(screen.queryByRole('alert')).toBeNull()
})
