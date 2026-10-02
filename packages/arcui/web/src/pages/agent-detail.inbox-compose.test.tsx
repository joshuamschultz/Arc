// Item 57 — operator compose on the agent Inbox tab.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { InboxTab } from '@/pages/agent-detail'

const THREAD = {
  thread_id: 't1',
  subject: 'Hello',
  updated_at: '2026-10-01T00:00:00Z',
  unread_count: 0,
  participants: [{ participant_id: 'did:arc:olivia', role: 'agent' }],
}
const sender = { participant_id: 'did:arc:op', role: 'human' }

interface Opts {
  post?: { status: number; body: unknown }
  messages?: Array<Record<string, unknown>>
  reply?: { status: number; body: unknown }
}

function stubFetch(opts: Opts = {}) {
  const calls: Array<{ path: string; method: string; headers: Record<string, string>; body: unknown }> = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const method = init?.method ?? 'GET'
      const json = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
      if (method === 'POST') {
        calls.push({
          path,
          method,
          headers: (init?.headers ?? {}) as Record<string, string>,
          body: init?.body ? JSON.parse(String(init.body)) : undefined,
        })
        if (path.endsWith('/reply')) return json(opts.reply?.body ?? {}, opts.reply?.status ?? 201)
        const p = opts.post ?? {
          status: 201,
          body: { message_id: 'm1', conversation_id: 'c1', thread_id: 't1', status: 'sent' },
        }
        return json(p.body, p.status)
      }
      if (path.endsWith('/inbox/t1')) {
        return json({
          messages: opts.messages ?? [
            { message_id: 'm1', thread_id: 't1', body: 'first', created_at: '2026-10-01T00:00:00Z', sender },
          ],
          handoffs: [],
        })
      }
      if (path.includes('/inbox')) return json({ threads: [THREAD] })
      if (path.includes('/tasks')) return json({ tasks: [] })
      if (path.includes('/approvals')) return json({ approvals: [] })
      return json({ agents: [] })
    }),
  )
  return calls
}

function renderInbox() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TooltipProvider>
          <InboxTab agentId="olivia" />
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function compose() {
  fireEvent.click(await screen.findByRole('button', { name: 'New message' }))
  fireEvent.change(screen.getByLabelText('Mail subject'), { target: { value: 'Hello' } })
  fireEvent.change(screen.getByLabelText('Mail body'), { target: { value: 'Please look' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }))
}

beforeEach(() => localStorage.setItem('arcui_operator_mode', '1'))
afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.unstubAllGlobals()
})

describe('InboxTab compose', () => {
  it('sends with an Idempotency-Key and opens the new thread', async () => {
    const calls = stubFetch()
    renderInbox()
    await compose()
    expect(await screen.findByText('first')).toBeTruthy()
    const post = calls.find((c) => c.path.endsWith('/api/agents/olivia/inbox'))!
    expect(post.headers['Idempotency-Key']).toBeTruthy()
    expect(post.body).toEqual({ body: 'Please look', subject: 'Hello' })
  })

  it('shows pending honestly', async () => {
    stubFetch({
      post: { status: 201, body: { message_id: 'm1', conversation_id: 'c1', thread_id: 't1', status: 'pending' } },
    })
    renderInbox()
    await compose()
    expect(await screen.findByText(/pending/i)).toBeTruthy()
  })

  it('shows the 403 message', async () => {
    stubFetch({ post: { status: 403, body: { error: 'operator_role_required' } } })
    renderInbox()
    await compose()
    expect(await screen.findByText(/operator role is required/i)).toBeTruthy()
  })

  it('disables Reply on a thread that already has its reply', async () => {
    stubFetch({
      messages: [
        { message_id: 'm1', thread_id: 't1', body: 'first', created_at: '2026-10-01T00:00:00Z', sender },
        { message_id: 'm2', thread_id: 't1', body: 'answer', created_at: '2026-10-01T00:01:00Z', sender, reply_to_id: 'm1' },
      ],
    })
    renderInbox()
    fireEvent.click(await screen.findByText('Hello'))
    expect(await screen.findByText(/Continue in the team channel/)).toBeTruthy()
    const button = screen.getByRole('button', { name: 'Send reply' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
  })

  it('renders the channel hint on a 409 reply', async () => {
    stubFetch({ reply: { status: 409, body: { error: 'mail_thread_closed', detail: 'closed' } } })
    renderInbox()
    fireEvent.click(await screen.findByText('Hello'))
    fireEvent.change(await screen.findByLabelText('Reply to mail thread'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send reply' }))
    await waitFor(() => expect(screen.getAllByText(/Continue in the team channel/).length).toBeGreaterThan(0))
    expect((screen.getByRole('button', { name: 'Send reply' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
