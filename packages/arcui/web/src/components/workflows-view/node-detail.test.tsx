import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { NodeDetail, RunError } from '@/components/workflows-view/node-detail'
import type { WorkflowRunNodeStatus } from '@/lib/types'

afterEach(cleanup)

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
