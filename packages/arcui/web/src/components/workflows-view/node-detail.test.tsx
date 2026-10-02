import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { NodeDetail, RunError } from '@/components/workflows-view/node-detail'
import type { WorkflowRunNodeStatus } from '@/lib/types'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const node = (extra: Partial<WorkflowRunNodeStatus>): WorkflowRunNodeStatus => ({
  node_id: 'ingest',
  status: 'failed',
  ...extra,
})

describe('NodeDetail', () => {
  it('node row shows last_error attempts and io', () => {
    render(
      <NodeDetail
        node={node({
          last_error: 'tool timed out',
          attempts: 2,
          max_attempts: 3,
          input: { args: { path: '/Meetings' }, upstream: {} },
          output: { count: 4 },
        })}
      />,
    )
    expect(screen.getByText('tool timed out')).toBeTruthy()
    expect(screen.getByText('2/3 attempts')).toBeTruthy()
    expect(screen.getByText('Input')).toBeTruthy()
    expect(screen.getByText('Output')).toBeTruthy()
    expect(screen.getByText(/"count": 4/)).toBeTruthy()
  })

  it('shows the true size when a value was truncated', () => {
    render(
      <NodeDetail
        node={node({
          status: 'done',
          output: { truncated: true, size_bytes: 90000, preview: '{"a":1' },
        })}
      />,
    )
    expect(screen.getByText(/truncated.*90000 bytes/i)).toBeTruthy()
  })

  it('shows a withheld marker above clearance', () => {
    render(
      <NodeDetail
        node={node({
          status: 'done',
          input: { withheld: 'classification' },
          output: { withheld: 'classification' },
        })}
      />,
    )
    expect(screen.getAllByText(/withheld/i).length).toBe(2)
  })

  it('renders nothing for a node with no detail fields', () => {
    const { container } = render(<NodeDetail node={node({ status: 'skipped' })} />)
    expect(container.textContent).toBe('')
  })
})

describe('NodeDetail route, reason and retry', () => {
  const wrap = (ui: React.ReactElement) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
  }

  it('routed node shows the chosen route chip', () => {
    wrap(<NodeDetail node={node({ status: 'routed', route: 'urgent' })} />)
    expect(screen.getByText('chose route: urgent')).toBeTruthy()
  })

  it('skipped and cancelled nodes show their reason', () => {
    wrap(
      <>
        <NodeDetail node={node({ node_id: 'a', status: 'skipped', reason: 'upstream x failed: boom' })} />
        <NodeDetail node={node({ node_id: 'b', status: 'cancelled', reason: 'run cancelled' })} />
      </>,
    )
    expect(screen.getByText('upstream x failed: boom')).toBeTruthy()
    expect(screen.getByText('run cancelled')).toBeTruthy()
  })

  it.each([
    ['failed node, failed run, operator', 'failed', 'failed', true, true],
    ['done node', 'done', 'failed', true, false],
    ['run still running', 'failed', 'running', true, false],
    ['not operator', 'failed', 'failed', false, false],
  ] as const)('retry button: %s', (_label, nodeStatus, runStatus, canRetry, visible) => {
    wrap(
      <NodeDetail
        node={node({ status: nodeStatus, last_error: 'x' })}
        runId="r1"
        runStatus={runStatus}
        canRetry={canRetry}
      />,
    )
    expect(!!screen.queryByRole('button', { name: /retry node/i })).toBe(visible)
  })

  it('retry posts to the node retry route and shows errors inline', async () => {
    const calls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (path: RequestInfo | URL, init?: RequestInit) => {
        calls.push(`${init?.method} ${String(path)}`)
        return new Response(JSON.stringify({ error: 'node not retryable' }), { status: 409 })
      }),
    )
    wrap(<NodeDetail node={node({ status: 'failed' })} runId="r 1" runStatus="failed" canRetry />)
    await userEvent.click(screen.getByRole('button', { name: /retry node/i }))
    expect(await screen.findByText('node not retryable')).toBeTruthy()
    expect(calls).toEqual(['POST /api/workflow-runs/r%201/nodes/ingest/retry'])
  })
})

describe('RunError', () => {
  it('shows why a failed run failed', () => {
    render(<RunError status="failed" lastError="node ingest exhausted retries" />)
    expect(screen.getByText('node ingest exhausted retries')).toBeTruthy()
  })
  it('is silent without an error', () => {
    const { container } = render(<RunError status="done" lastError={null} />)
    expect(container.textContent).toBe('')
  })
})
