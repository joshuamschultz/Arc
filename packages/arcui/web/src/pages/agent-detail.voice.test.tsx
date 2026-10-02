// Item 13 — the voice card shows live status, a persisted Listening switch, and a typed wake word.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { TooltipProvider } from '@/components/ui/tooltip'
import { VoicePanel } from '@/pages/agent-detail'

const part = (up: boolean, reason = '') => ({ up, heartbeat_at: Date.now() / 1000, reason })

function live(over: Record<string, unknown> = {}) {
  return {
    listening: true,
    state: 'listening',
    client_connected: true,
    reason: '',
    mode: 'stt-wake',
    mic: 'plughw:CARD=MV7i,DEV=0',
    wake_words: ['olivia'],
    wake_mode: 'stt',
    wake_loaded: true,
    last_wake_at: null,
    adapter: part(true),
    client: part(true),
    engine: part(true),
    test_heard: null,
    ...over,
  }
}

interface Calls {
  posts: Array<{ path: string; body: unknown }>
}

function stubFetch(status: Record<string, unknown>, postResult: { status: number; body: unknown } = { status: 200, body: {} }): Calls {
  const calls: Calls = { posts: [] }
  vi.stubGlobal(
    'fetch',
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const path = String(request)
      const json = (body: unknown, code = 200) =>
        new Response(JSON.stringify(body), { status: code, headers: { 'Content-Type': 'application/json' } })
      if ((init?.method ?? 'GET') === 'POST') {
        calls.posts.push({ path, body: init?.body ? JSON.parse(String(init.body)) : undefined })
        return json(postResult.body, postResult.status)
      }
      if (path.endsWith('/api/agents/olivia/voice')) {
        return json({ enabled: true, bound_to_this_agent: true, tts: 'kokoro', stt: 'whisper', live: live(), ...status })
      }
      return json({})
    }),
  )
  return calls
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TooltipProvider>
          <VoicePanel agentId="olivia" />
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => localStorage.setItem('arcui_operator_mode', '1'))
afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.unstubAllGlobals()
})

describe('VoicePanel live status', () => {
  it('shows Listening with all three parts up', async () => {
    stubFetch({})
    renderPanel()
    const status = await screen.findByRole('status', { name: 'Voice status' })
    expect(status.textContent).toContain('Listening')
    expect(screen.getByText('Gateway adapter')).toBeTruthy()
    expect(screen.getByText('Mic client (arc-voice)')).toBeTruthy()
    expect(screen.getByText('Speech engine')).toBeTruthy()
    expect(screen.getAllByText('Up')).toHaveLength(3)
  })

  it('shows Offline with the plain reason and the command to run', async () => {
    stubFetch({
      live: live({
        state: 'offline',
        client_connected: false,
        reason: 'No mic client is connected. Run arc-voice on the box with the microphone.',
        client: part(false, 'No mic client is connected.'),
      }),
    })
    renderPanel()
    const status = await screen.findByRole('status', { name: 'Voice status' })
    expect(status.textContent).toContain('Offline')
    expect(screen.getAllByText(/No mic client is connected/).length).toBeGreaterThan(0)
    expect(screen.getByText('systemctl --user enable --now arc-voice')).toBeTruthy()
    expect(screen.getByText('Down')).toBeTruthy()
  })

  it('shows Paused when listening is off', async () => {
    stubFetch({ live: live({ listening: false, state: 'paused' }) })
    renderPanel()
    const status = await screen.findByRole('status', { name: 'Voice status' })
    expect(status.textContent).toContain('Paused')
    expect(screen.getByRole('switch', { name: 'Listening' }).getAttribute('aria-checked')).toBe('false')
  })
})

describe('VoicePanel controls', () => {
  it('the switch posts the opposite listening state', async () => {
    const calls = stubFetch({})
    renderPanel()
    fireEvent.click(await screen.findByRole('switch', { name: 'Listening' }))
    await waitFor(() => expect(calls.posts.length).toBe(1))
    expect(calls.posts[0].path).toBe('/api/agents/olivia/voice/listening')
    expect(calls.posts[0].body).toEqual({ on: false })
  })

  it('the switch is disabled outside operator mode', async () => {
    localStorage.clear()
    stubFetch({})
    renderPanel()
    const toggle = await screen.findByRole('switch', { name: 'Listening' })
    expect((toggle as HTMLButtonElement).disabled).toBe(true)
  })

  it('saves typed wake words as a list and states what a custom word is', async () => {
    const calls = stubFetch({}, { status: 200, body: { note: 'Not a trained wake-word model.' } })
    renderPanel()
    expect(await screen.findByText(/not a trained wake-word model/i)).toBeTruthy()
    fireEvent.change(await screen.findByLabelText('Wake word'), { target: { value: 'Computer, hey jarvis' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.posts.length).toBe(1))
    expect(calls.posts[0].path).toBe('/api/agents/olivia/voice/wake')
    expect(calls.posts[0].body).toEqual({ words: ['computer', 'hey jarvis'] })
  })

  it('shows the server validation message for a bad word', async () => {
    stubFetch({}, { status: 400, body: { error: 'a wake word is 2-32 characters' } })
    renderPanel()
    fireEvent.change(await screen.findByLabelText('Wake word'), { target: { value: 'bad:word' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(/2-32 characters/)).toBeTruthy()
  })

  it('shows the transcript only while a test is running', async () => {
    stubFetch({ live: live({ test_heard: 'hello there' }) })
    renderPanel()
    expect(await screen.findByText('hello there')).toBeTruthy()
  })
})
