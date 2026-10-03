// Item 20 P20-6 — the run drawer lists the signed audit rows written under that run.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { RunDetailDrawer } from '@/components/run-detail-drawer'
import type { RunSummary } from '@/lib/types'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const run = {
  run_id: 'run-1',
  agent: 'olivia',
  turns: 2,
  tool_calls: 1,
  status: 'completed',
} as unknown as RunSummary

describe('RunDetailDrawer audit events', () => {
  it('lists the audit rows for the run with their verified state', async () => {
    const paths: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (request: RequestInfo | URL) => {
        const path = String(request)
        paths.push(path)
        const json = (body: unknown) =>
          new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
        if (path === '/api/runs/run-1/audit') {
          return json({
            run_id: 'run-1',
            events: [
              { seq: 3, action: 'policy.evaluate', action_label: 'Tool policy check', outcome: 'allow', verified: true, signature: 's', tool_call_id: 'tc-1' },
              { seq: 4, action: 'tool.executed', action_label: 'Tool executed', outcome: 'ok', verified: false, signature: 's' },
            ],
          })
        }
        if (path.endsWith('/timeline')) return json({ run_id: 'run-1', timeline: [] })
        return json({ events: [] })
      }),
    )
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <RunDetailDrawer run={run} open onOpenChange={() => {}} />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    const section = await screen.findByRole('region', { name: /audit events/i })
    expect(section.textContent).toContain('Tool policy check')
    expect(section.textContent).toContain('Tool executed')
    expect(paths).toContain('/api/runs/run-1/audit')
  })
})
