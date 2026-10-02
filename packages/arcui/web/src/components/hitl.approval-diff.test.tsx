import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ApprovalRequest } from '@/components/hitl'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('approval card shows the reason and an expandable diff', () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ agents: [] }))))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <ApprovalRequest
        operatorMode
        a={{
          id: 'a1',
          agent_did: 'did:arc:x',
          agent_label: 'olivia',
          tool: 'workflow.sign',
          legs: [],
          call_hash: 'h',
          status: 'pending',
          created_at: '2026-10-02T10:00:00Z',
          expires_at: '2026-10-03T10:00:00Z',
          reason: 'New version adds a send node',
          diff: { nodes: { added: ['send'] }, files: [] },
        }}
      />
    </QueryClientProvider>,
  )
  expect(screen.getByText('New version adds a send node')).toBeTruthy()
  expect(screen.getByText('What changes')).toBeTruthy()
  expect(screen.getByText('send')).toBeTruthy()
})
