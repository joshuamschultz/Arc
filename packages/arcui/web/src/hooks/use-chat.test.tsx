import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useChatSession } from './use-chat'

vi.mock('@/lib/api', () => ({ apiGet: vi.fn(() => Promise.resolve({ messages: [] })) }))
vi.mock('@/lib/auth', () => ({ getToken: () => 'tok' }))

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

class FakeSocket {
  static OPEN = 1
  static last: FakeSocket | null = null
  readyState = 1
  sent: string[] = []
  private listeners: Record<string, ((ev: { data?: string }) => void)[]> = {}
  url: string
  constructor(url: string) {
    this.url = url
    FakeSocket.last = this
  }
  addEventListener(type: string, fn: (ev: { data?: string }) => void) {
    ;(this.listeners[type] ??= []).push(fn)
  }
  send(data: string) {
    this.sent.push(data)
  }
  close() {}
  emit(type: string, data?: unknown) {
    for (const fn of this.listeners[type] ?? []) fn({ data: data === undefined ? undefined : JSON.stringify(data) })
  }
}

describe('useChatSession', () => {
  const originalRandomUUID = crypto.randomUUID

  beforeEach(() => {
    vi.stubGlobal('WebSocket', FakeSocket)
  })

  afterEach(() => {
    Object.defineProperty(crypto, 'randomUUID', { value: originalRandomUUID, configurable: true })
    vi.unstubAllGlobals()
  })

  function openSession() {
    const hook = renderHook(() => useChatSession('josh_agent'))
    const ws = FakeSocket.last!
    act(() => {
      ws.emit('open')
      ws.emit('message', { type: 'ready', chat_id: 'abc' })
    })
    return { hook, ws }
  }

  it('sends a canonical UUIDv4 request id outside a secure context', () => {
    // Plain-http dashboards (e.g. http://<tailnet-ip>:8420) are not a secure
    // context: the browser leaves crypto.randomUUID undefined there.
    Object.defineProperty(crypto, 'randomUUID', { value: undefined, configurable: true })
    const { hook, ws } = openSession()

    let sent = false
    act(() => {
      sent = hook.result.current.sendMessage('hello')
    })

    expect(sent).toBe(true)
    const frame = JSON.parse(ws.sent[ws.sent.length - 1])
    expect(frame.type).toBe('message')
    expect(frame.request_id).toMatch(UUID_V4)
    expect(hook.result.current.messages.some((m) => m.role === 'user' && m.text === 'hello')).toBe(true)
  })

  it('shows a typed server error frame instead of swallowing it', () => {
    const { hook, ws } = openSession()
    act(() => {
      ws.emit('message', { type: 'error', code: 'malformed', message: 'replay' })
    })
    expect(hook.result.current.messages.some((m) => m.role === 'system' && m.text.includes('replay'))).toBe(true)
  })

  it('shows a failed run instead of leaving the turn silent', () => {
    const { hook, ws } = openSession()
    act(() => {
      ws.emit('message', { type: 'stream', event: 'end', run_id: 'r1', status: 'failed', reason: 'The agent did not start in time.' })
    })
    expect(
      hook.result.current.messages.some((m) => m.role === 'system' && m.text.includes('did not start in time')),
    ).toBe(true)
  })

  it('on return mid-run shows the pending user message and a working flag, until the run ends', async () => {
    const { apiGet } = await import('@/lib/api')
    vi.mocked(apiGet).mockResolvedValueOnce({
      messages: [
        { role: 'assistant', content: 'old answer', timestamp: 't0' },
        { role: 'user', content: 'my new question', timestamp: 't1' },
      ],
      run_in_flight: true,
    })
    const { hook, ws } = openSession()
    await act(async () => {
      await Promise.resolve()
    })

    expect(hook.result.current.messages.map((m) => m.text)).toEqual(['old answer', 'my new question'])
    expect(hook.result.current.working).toBe(true)

    act(() => {
      ws.emit('message', { type: 'stream', event: 'end', run_id: 'r9', status: 'completed' })
    })
    expect(hook.result.current.working).toBe(false)
  })

  it('is not working when the session has no run in flight', async () => {
    const { hook } = openSession()
    await act(async () => {
      await Promise.resolve()
    })
    expect(hook.result.current.working).toBe(false)
  })

  it('shows an answer that finished while the tab was away once the socket returns', async () => {
    vi.useFakeTimers()
    try {
      const { apiGet } = await import('@/lib/api')
      const { hook, ws } = openSession()
      vi.mocked(apiGet).mockResolvedValueOnce({
        messages: [{ role: 'assistant', content: 'finished research answer', timestamp: 't' }],
      })
      act(() => ws.emit('close'))
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000)
      })
      const returned = FakeSocket.last!
      expect(returned).not.toBe(ws)
      await act(async () => {
        returned.emit('open')
        returned.emit('message', { type: 'ready', chat_id: 'abc' })
        await Promise.resolve()
      })
      expect(hook.result.current.messages.map((m) => m.text)).toContain('finished research answer')
    } finally {
      vi.useRealTimers()
    }
  })
})
