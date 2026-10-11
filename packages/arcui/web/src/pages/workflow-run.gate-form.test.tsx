// SPEC-088 T-1377 (COMP-016, REQ-617): the gate panel on a run page.
//
// - A waiting gate whose run node carries `output_schema_resolved` with a flat
//   object schema (string, number, integer, boolean, enum) renders one labelled
//   control per property; Approve posts the typed payload.
// - A schema the form cannot render flat (nested object, array) falls back to a
//   raw JSON editor; Approve posts the parsed object.
// - A human_input node shows its `prompt` and the form, with Submit and Fail run
//   (decisions `submit` / `fail_run`), never Approve.
// - A 422 names the failing field next to it; the step stays decidable.
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { WorkflowRunPage } from './workflow-run'

// The run graph (xyflow) measures its container with ResizeObserver.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const FLAT_SCHEMA = {
  type: 'object',
  properties: {
    qty: { type: 'integer', title: 'Quantity' },
    price: { type: 'number' },
    memo: { type: 'string', description: 'Why you approved' },
    rush: { type: 'boolean' },
    tier: { type: 'string', enum: ['silver', 'gold'] },
  },
  required: ['qty'],
}

const NESTED_SCHEMA = {
  type: 'object',
  properties: { address: { type: 'object', properties: { city: { type: 'string' } } } },
}

const PROMISE_SCHEMA = {
  type: 'object',
  properties: { promised: { type: 'string' } },
  required: ['promised'],
}

const WORKFLOW = {
  id: 'orders',
  name: 'Orders',
  status: 'signed',
  version: 1,
  nodes: [
    { id: 'approve', kind: 'gate' },
    { id: 'ask', kind: 'human_input', needs: ['approve'] },
  ],
  edges: [{ from: 'approve', to: 'ask' }],
}

function runWith(node: Record<string, unknown>) {
  return {
    run_id: 'run-1',
    workflow_id: 'orders',
    version: 1,
    status: 'waiting_gate',
    started_at: '2026-10-10T10:00:00+00:00',
    path_taken: [node.node_id],
    nodes: [{ status: 'waiting_gate', task_id: 'task-1', ...node }],
  }
}

interface Post {
  path: string
  body: Record<string, unknown>
}

function stubApi(run: unknown, gateReply: { status: number; body: unknown } = { status: 200, body: {} }) {
  const posts: Post[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: RequestInfo | URL, init?: RequestInit) => {
      const p = String(path)
      const reply = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
      if (init?.method === 'POST') {
        posts.push({ path: p, body: JSON.parse(String(init.body ?? '{}')) })
        return reply(gateReply.body, gateReply.status)
      }
      if (p === '/api/workflows/orders') return reply(WORKFLOW)
      if (p === '/api/workflow-runs/run-1') return reply(run)
      return reply({ agents: [], runs: [], versions: [] })
    }),
  )
  return posts
}

function renderRun() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={['/workflows/orders/runs/run-1']}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/workflows/:id/runs/:runId" element={<WorkflowRunPage />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => localStorage.setItem('arcui_operator_mode', '1'))

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

it('a flat schema renders a typed form and Approve posts the typed payload', async () => {
  const posts = stubApi(
    runWith({ node_id: 'approve', kind: 'gate', output_schema_resolved: FLAT_SCHEMA }),
  )
  renderRun()

  const qty = await screen.findByLabelText(/quantity/i)
  await userEvent.type(qty, '3')
  await userEvent.type(screen.getByLabelText(/^price/i), '9.5')
  await userEvent.type(screen.getByLabelText(/^memo/i), 'looks right')
  await userEvent.click(screen.getByRole('checkbox', { name: /rush/i }))
  await userEvent.selectOptions(screen.getByRole('combobox', { name: /tier/i }), 'gold')
  expect(screen.getByText('Why you approved')).toBeTruthy()
  // No raw JSON editor when the form can carry the schema.
  expect(screen.queryByLabelText(/payload \(json\)/i)).toBeNull()

  await userEvent.click(screen.getByRole('button', { name: /^approve/i }))
  await waitFor(() => expect(posts).toHaveLength(1))
  expect(posts[0].path).toBe('/api/workflow-tasks/task-1/gate')
  expect(posts[0].body).toEqual({
    decision: 'approve',
    payload: { qty: 3, price: 9.5, memo: 'looks right', rush: true, tier: 'gold' },
  })
})

it('a required field left empty is named before anything is sent', async () => {
  const posts = stubApi(
    runWith({ node_id: 'approve', kind: 'gate', output_schema_resolved: FLAT_SCHEMA }),
  )
  renderRun()

  await screen.findByLabelText(/quantity/i)
  await userEvent.click(screen.getByRole('button', { name: /^approve/i }))
  expect(await screen.findByText(/quantity is required/i)).toBeTruthy()
  expect(posts).toHaveLength(0)
})

it('a nested schema falls back to a raw JSON editor', async () => {
  const posts = stubApi(
    runWith({ node_id: 'approve', kind: 'gate', output_schema_resolved: NESTED_SCHEMA }),
  )
  renderRun()

  const editor = await screen.findByLabelText(/payload \(json\)/i)
  await userEvent.clear(editor)
  // userEvent treats `{` as a key descriptor; `{{` types a literal brace.
  await userEvent.type(editor, '{{"address": {{"city": "Austin"}}')
  await userEvent.click(screen.getByRole('button', { name: /^approve/i }))
  await waitFor(() => expect(posts).toHaveLength(1))
  expect(posts[0].body).toEqual({ decision: 'approve', payload: { address: { city: 'Austin' } } })
})

it('invalid JSON in the raw editor is refused before anything is sent', async () => {
  const posts = stubApi(
    runWith({ node_id: 'approve', kind: 'gate', output_schema_resolved: NESTED_SCHEMA }),
  )
  renderRun()

  const editor = await screen.findByLabelText(/payload \(json\)/i)
  await userEvent.clear(editor)
  await userEvent.type(editor, 'not json')
  await userEvent.click(screen.getByRole('button', { name: /^approve/i }))
  expect(await screen.findByText(/not valid json/i)).toBeTruthy()
  expect(posts).toHaveLength(0)
})

it('a gate without a schema keeps the plain approve card', async () => {
  const posts = stubApi(runWith({ node_id: 'approve', kind: 'gate' }))
  renderRun()

  await userEvent.click(await screen.findByRole('button', { name: /^approve/i }))
  await waitFor(() => expect(posts).toHaveLength(1))
  expect(posts[0].body).toEqual({ decision: 'approve' })
})

it('a human_input step shows its prompt and submits the form', async () => {
  const posts = stubApi(
    runWith({
      node_id: 'ask',
      kind: 'human_input',
      prompt: 'Confirm the promised date',
      output_schema_resolved: PROMISE_SCHEMA,
    }),
  )
  renderRun()

  expect(await screen.findByText('Confirm the promised date')).toBeTruthy()
  expect(screen.queryByRole('button', { name: /^approve/i })).toBeNull()
  expect(screen.queryByRole('button', { name: /return for revision/i })).toBeNull()
  await userEvent.type(screen.getByLabelText(/^promised/i), '2026-11-01')
  await userEvent.click(screen.getByRole('button', { name: /^submit/i }))
  await waitFor(() => expect(posts).toHaveLength(1))
  expect(posts[0].body).toEqual({ decision: 'submit', payload: { promised: '2026-11-01' } })
})

it('a human_input step can fail the run without a payload', async () => {
  const posts = stubApi(
    runWith({
      node_id: 'ask',
      kind: 'human_input',
      prompt: 'Confirm the promised date',
      output_schema_resolved: PROMISE_SCHEMA,
    }),
  )
  renderRun()

  await userEvent.click(await screen.findByRole('button', { name: /fail run/i }))
  await waitFor(() => expect(posts).toHaveLength(1))
  expect(posts[0].body).toEqual({ decision: 'fail_run' })
})

it('a 422 names the failing field next to it', async () => {
  stubApi(
    runWith({
      node_id: 'ask',
      kind: 'human_input',
      prompt: 'Confirm the promised date',
      output_schema_resolved: PROMISE_SCHEMA,
    }),
    {
      status: 422,
      body: { errors: [{ node_id: '', field: 'payload.promised', error: 'must be a date' }] },
    },
  )
  renderRun()

  await userEvent.type(await screen.findByLabelText(/^promised/i), 'soon')
  await userEvent.click(screen.getByRole('button', { name: /^submit/i }))
  expect(await screen.findByText('must be a date')).toBeTruthy()
  expect(screen.getByLabelText(/^promised/i).getAttribute('aria-invalid')).toBe('true')
  // Still decidable: the buttons stay.
  expect(screen.getByRole('button', { name: /^submit/i })).toBeTruthy()
})
