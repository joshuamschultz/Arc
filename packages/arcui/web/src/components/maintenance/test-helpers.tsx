// Shared harness for the Maintenance section tests: the server is stubbed at the
// network boundary (global fetch), so each section runs through its real hooks.
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { vi } from 'vitest'
import type { ReactElement } from 'react'

export type Call = { path: string; method: string; body: unknown }

type Reply = unknown | { status: number; body: unknown }
type Handler = (call: Call) => Reply

const isStatusReply = (r: Reply): r is { status: number; body: unknown } =>
  typeof r === 'object' && r !== null && 'status' in r && 'body' in r

/** Stub `fetch`. Keys are `"METHOD /path"`; the first key contained in the request wins. */
export function stubApi(handlers: Record<string, Handler | Reply>): Call[] {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const method = (init?.method ?? 'GET').toUpperCase()
      let body: unknown = undefined
      if (typeof init?.body === 'string') {
        try {
          body = JSON.parse(init.body)
        } catch {
          body = init.body
        }
      }
      const call = { path, method, body }
      calls.push(call)
      const key = Object.keys(handlers).find((k) => {
        const [m, p] = k.split(' ')
        return m === method && path.includes(p)
      })
      const handler = key ? handlers[key] : undefined
      const reply = typeof handler === 'function' ? (handler as Handler)(call) : (handler ?? {})
      const status = isStatusReply(reply) ? reply.status : 200
      const payload = isStatusReply(reply) ? reply.body : reply
      return new Response(JSON.stringify(payload), {
        status,
        headers: { 'Content-Type': 'application/json' },
      })
    }),
  )
  return calls
}

export function renderWithClient(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}
