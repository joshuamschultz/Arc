import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { WorkflowNodeForm } from '@/components/workflow-node-form'
import { toDraft } from '@/lib/workflow-node-draft'
import type { WorkflowNode } from '@/lib/types'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function mountWith(tool: string) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: RequestInfo | URL) => {
      const p = String(path)
      if (p === '/api/team/roster') {
        return new Response(JSON.stringify({ agents: [{ agent_id: 'sales', name: 'sales' }] }))
      }
      if (p === '/api/agents/sales/tools') {
        return new Response(
          JSON.stringify({
            tools: [
              { name: 'send_mail', idempotent: false },
              { name: 'list_mail', idempotent: true },
            ],
            allowlist: [],
            denylist: [],
            policy_summary: {},
          }),
        )
      }
      return new Response(JSON.stringify({ items: [] }))
    }),
  )
  const node: WorkflowNode = { id: 'send', kind: 'tool', agent: '@sales', tool } as WorkflowNode
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <WorkflowNodeForm draft={toDraft(node)} siblings={[node]} onChange={() => {}} />
    </QueryClientProvider>,
  )
}

it('node form flags non-idempotent tools', async () => {
  mountWith('send_mail')
  expect(await screen.findByText(/repeat is not safe/i)).toBeTruthy()
  expect(screen.getByText(/retry.*run.*side effect again/i)).toBeTruthy()
})

it('node form shows no flag for a safe tool', async () => {
  mountWith('list_mail')
  await screen.findByText('Tool')
  await new Promise((resolve) => setTimeout(resolve, 20))
  expect(screen.queryByText(/repeat is not safe/i)).toBeNull()
})
