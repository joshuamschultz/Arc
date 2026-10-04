import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { WorkflowDetailPage } from './workflow-detail'
import { WorkflowRunPage } from './workflow-run'

// The run graph (xyflow) measures its container with ResizeObserver.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const RESTART = 'interrupted: the Arc service restarted while this step was running'

const WORKFLOW = {
  id: 'ingest',
  name: 'Meeting Ingestion',
  status: 'signed',
  version: 30,
  nodes: [
    { id: 'list_dropbox', kind: 'tool' },
    { id: 'filter_new', kind: 'agent', needs: ['list_dropbox'] },
    { id: 'notify', kind: 'agent', needs: ['filter_new'] },
  ],
  edges: [
    { from: 'list_dropbox', to: 'filter_new' },
    { from: 'filter_new', to: 'notify' },
  ],
}

const REASON = {
  node_id: 'filter_new',
  summary: 'The Arc service restarted while this step was running. Tried 3 times.',
  detail: RESTART,
}

const RUN = {
  run_id: 'schedule_3464',
  workflow_id: 'ingest',
  version: 30,
  status: 'failed',
  started_at: '2026-10-04T03:00:09+00:00',
  ended_at: '2026-10-04T03:09:54+00:00',
  last_error: `node filter_new failed: ${RESTART}`,
  failure_reason: REASON,
  path_taken: ['list_dropbox', 'filter_new'],
  nodes: [
    {
      node_id: 'list_dropbox',
      status: 'done',
      kind: 'tool',
      started_at: '2026-10-04T03:00:21+00:00',
      completed_at: '2026-10-04T03:00:33+00:00',
      duration_s: 12,
      attempts: 2,
      max_attempts: 3,
      last_error: null,
      recovered_from: { summary: 'A service the step called did not answer in time.', detail: 'ReadTimeout: ' },
    },
    {
      node_id: 'filter_new',
      status: 'failed',
      kind: 'agent',
      task_run_id: 'eada34b3-ad69-a4fb-565a-fb67466e64a8',
      started_at: '2026-10-04T03:07:09+00:00',
      completed_at: '2026-10-04T03:09:35+00:00',
      duration_s: 146,
      attempts: 3,
      max_attempts: 3,
      last_error: RESTART,
      error_summary: 'The Arc service restarted while this step was running.',
    },
    {
      node_id: 'notify',
      status: 'cancelled',
      reason: `upstream filter_new failed: ${RESTART}`,
      deliver_to: 'telegram:8293394811',
    },
  ],
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

function stubApi(posts: string[] = []) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: RequestInfo | URL, init?: RequestInit) => {
      const p = String(path)
      if (init?.method === 'POST') {
        posts.push(p)
        return new Response(JSON.stringify({ run_id: RUN.run_id, status: 'running', failure_reason: null }))
      }
      if (p === '/api/workflows/ingest') return new Response(JSON.stringify(WORKFLOW))
      if (p === '/api/workflows/ingest/runs') {
        return new Response(
          JSON.stringify({
            runs: [
              { run_id: RUN.run_id, status: 'failed', started_at: RUN.started_at, failure_reason: REASON },
              { run_id: 'run-ok', status: 'done', started_at: RUN.started_at, failure_reason: null },
            ],
          }),
        )
      }
      if (p === `/api/workflow-runs/${RUN.run_id}`) return new Response(JSON.stringify(RUN))
      return new Response(JSON.stringify({ agents: [], runs: [], versions: [] }))
    }),
  )
}

function Where() {
  const location = useLocation()
  return <span data-testid="where">{location.pathname + location.search}</span>
}

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/workflows/:id" element={<WorkflowDetailPage />} />
          <Route path="/workflows/:id/runs/:runId" element={<WorkflowRunPage />} />
        </Routes>
        <Where />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

it('the run list says which node failed and why, and a row opens the run page', async () => {
  stubApi()
  renderAt('/workflows/ingest?tab=runs')

  const reason = await screen.findByText(
    'filter_new: The Arc service restarted while this step was running. Tried 3 times.',
  )
  const row = reason.closest('tr')!
  expect(within(row).getByText(/failed/i)).toBeTruthy()

  await userEvent.click(row)
  expect(screen.getByTestId('where').textContent).toBe('/workflows/ingest/runs/schedule_3464')
})

it('the run page shows why it failed and a per-node timeline with plain reasons', async () => {
  stubApi()
  renderAt('/workflows/ingest/runs/schedule_3464')

  const banner = await screen.findByRole('alert')
  expect(banner.textContent).toContain('Why this run failed')
  expect(banner.textContent).toContain(
    'filter_new: The Arc service restarted while this step was running. Tried 3 times.',
  )

  // The plain sentence is shown; the raw error is folded under "Technical detail".
  expect(screen.getByText('The Arc service restarted while this step was running.')).toBeTruthy()
  expect(screen.getAllByText('Technical detail').length).toBeGreaterThan(0)
  expect(screen.getByText('3/3 attempts')).toBeTruthy()
  // A node that succeeded after a timeout is not shown as a red failure.
  expect(
    screen.getByText(
      'Succeeded after an earlier attempt failed: A service the step called did not answer in time.',
    ),
  ).toBeTruthy()
  expect(screen.getByText(/upstream filter_new failed/)).toBeTruthy()
  expect(screen.getByText('Delivers to telegram:8293394811')).toBeTruthy()
  const trace = screen.getByRole('link', { name: /agent run/i })
  expect(trace.getAttribute('href')).toBe('/arcrun?run=eada34b3-ad69-a4fb-565a-fb67466e64a8')
})

it('an operator retries from the failed node', async () => {
  localStorage.setItem('arcui_operator_mode', '1')
  const posts: string[] = []
  stubApi(posts)
  renderAt('/workflows/ingest/runs/schedule_3464')

  await userEvent.click(await screen.findByRole('button', { name: /retry node/i }))

  await waitFor(() =>
    expect(posts).toEqual(['/api/workflow-runs/schedule_3464/nodes/filter_new/retry']),
  )
})

it('a viewer is not offered retry', async () => {
  stubApi()
  renderAt('/workflows/ingest/runs/schedule_3464')

  await screen.findByRole('alert')
  expect(screen.queryByRole('button', { name: /retry node/i })).toBeNull()
})
