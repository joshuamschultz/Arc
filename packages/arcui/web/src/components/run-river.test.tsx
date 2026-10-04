// Context prep: the rows emitted before the first model turn fold into one step group.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RunRiver } from '@/components/run-river'
import type { RunSummary, TimelineEntry } from '@/lib/types'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const run = { run_id: 'run-1', agent: 'olivia', turns: 1, tool_calls: 0, status: 'completed' } as unknown as RunSummary

const item = (over: Record<string, unknown>) => ({
  source_kind: 'memory',
  source: 'cards/people',
  title: 'Alice',
  path: 'people/alice.md',
  score: 0.91,
  classification: 'internal',
  snippet: 'met last week',
  included: true,
  reason: 'top score',
  ...over,
})

const retrieval = (extra: Record<string, unknown> = {}) => ({
  status: 'ok',
  query: 'who is alice',
  latency_ms: 120,
  budget_ms: 800,
  top_k: 5,
  token_cap: 2000,
  tokens_injected: 340,
  steps: [],
  items: [item({}), item({ title: 'Bob', path: 'people/bob.md', included: false, reason: 'below threshold', score: 0.2 })],
  ...extra,
})

const prepRows = (retrievalExtra: Record<string, unknown> = {}): TimelineEntry[] => [
  { kind: 'run_event', name: 'strategy.selected', ts: '2026-10-04T10:00:00Z', extra: { strategy: 'react', reason: 'simple question', latency_ms: 30 } },
  { kind: 'run_event', name: 'context.system', ts: '2026-10-04T10:00:01Z', extra: { cached: true, tokens: 1200, sha256: 'abc' } },
  { kind: 'run_event', name: 'context.retrieval', ts: '2026-10-04T10:00:02Z', extra: retrieval(retrievalExtra) },
  { kind: 'run_event', name: 'context.session', ts: '2026-10-04T10:00:03Z', extra: { turns: 4, tokens: 900 } },
  { kind: 'llm_call', ts: '2026-10-04T10:00:04Z', model: 'claude-sonnet', prompt_tokens: 10, completion_tokens: 5 },
]

function renderRiver(timeline: TimelineEntry[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(JSON.stringify({ run_id: 'run-1', timeline }), { status: 200, headers: { 'Content-Type': 'application/json' } })),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <RunRiver run={run} />
    </QueryClientProvider>,
  )
}

describe('RunRiver context prep', () => {
  it('shows one collapsed Context prep group with the headline facts', async () => {
    renderRiver(prepRows())
    const header = await screen.findByRole('button', { name: /context prep/i })
    expect(header.textContent).toContain('strategy react')
    expect(header.textContent).toContain('retrieval ok')
    expect(header.textContent).toContain('1/2 items')
    expect(header.textContent).toContain('340 tok')
    expect(screen.queryByText('Alice')).toBeNull()
  })

  it('lists each retrieved item as used or excluded with its reason when expanded', async () => {
    renderRiver(prepRows())
    await userEvent.click(await screen.findByRole('button', { name: /context prep/i }))
    expect(screen.getByText('Alice')).toBeTruthy()
    expect(screen.getByText('top score')).toBeTruthy()
    expect(screen.getByText('Bob')).toBeTruthy()
    expect(screen.getByText('below threshold')).toBeTruthy()
    expect(screen.getByText('used')).toBeTruthy()
    expect(screen.getByText('excluded')).toBeTruthy()
    expect(screen.getByText(/simple question/)).toBeTruthy()
    expect(screen.getByText(/cached: yes/)).toBeTruthy()
    expect(screen.getByText(/4 turns/)).toBeTruthy()
  })

  it('says which step was skipped after a timeout', async () => {
    renderRiver([
      ...prepRows({ status: 'timeout', items: [] }).slice(0, 4),
      { kind: 'run_event', name: 'context.skipped', ts: '2026-10-04T10:00:03Z', extra: { step: 'retrieval', reason: 'over budget' } },
    ])
    await userEvent.click(await screen.findByRole('button', { name: /context prep/i }))
    expect(screen.getByText(/skipped: retrieval timed out/)).toBeTruthy()
  })

  it('shows run.not_started as a failed step with its reason', async () => {
    renderRiver([
      { kind: 'run_event', name: 'run.not_started', outcome: 'failed', ts: '2026-10-04T10:00:00Z', extra: { reason: 'policy denied the run' } },
    ])
    expect(await screen.findByText('Run did not start')).toBeTruthy()
    expect(screen.getByText('policy denied the run')).toBeTruthy()
    expect(screen.getByText('failed')).toBeTruthy()
  })

  it('renders a snippet containing markup as plain text', async () => {
    renderRiver(prepRows({ items: [item({ snippet: '<script>alert(1)</script>' })] }))
    await userEvent.click(await screen.findByRole('button', { name: /context prep/i }))
    expect(screen.getByText('<script>alert(1)</script>')).toBeTruthy()
    expect(document.querySelector('script')).toBeNull()
  })
})
