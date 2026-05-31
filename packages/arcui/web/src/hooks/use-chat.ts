import { useCallback, useEffect, useRef, useState } from 'react'
import { apiGet } from '@/lib/api'
import { getToken } from '@/lib/auth'
import type { Dict, SessionReplayResponse } from '@/lib/types'

export type ChatRole = 'user' | 'agent' | 'tool_call' | 'system'

export interface ChatMessage {
  id: string
  role: ChatRole
  text: string
  tool?: string
  time: string
}

export type ChatStatus = 'connecting' | 'ready' | 'reconnecting' | 'closed'

const RECONNECT_MAX_WINDOW_MS = 60_000
const BASE_DELAY = 800
const MAX_DELAY = 15_000

function now(): string {
  return new Date().toLocaleTimeString()
}

/**
 * Per-agent chat session over `/ws/chat/{agentId}`. Ports the old
 * messages-page protocol: first-message token auth, `ready` handshake,
 * monotonic `seq` gap detection (reconnect with `?since_seq=`), backoff with
 * a deadline window, history preload from the session log, and `client_seq`
 * on outbound messages.
 */
export function useChatSession(agentId: string | null) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [status, setStatus] = useState<ChatStatus>('connecting')

  const wsRef = useRef<WebSocket | null>(null)
  const lastSeq = useRef(-1)
  const clientSeq = useRef(0)
  const chatId = useRef<string | null>(null)
  const attempts = useRef(0)
  const deadline = useRef(0)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const historyLoaded = useRef(false)

  const append = useCallback((m: ChatMessage) => {
    setMessages((prev) => [...prev, m])
  }, [])

  const loadHistory = useCallback(
    async (agent: string, sid: string) => {
      if (historyLoaded.current) return
      try {
        const data = await apiGet<SessionReplayResponse>(
          `/api/agents/${agent}/sessions/${sid}?page_size=200`,
        )
        if (historyLoaded.current) return
        const hist: ChatMessage[] = data.messages.map((m: Dict, i) => ({
          id: `h${i}`,
          role: ((m.role as string) || (m.from as string) || 'agent') as ChatRole,
          text: String(m.text ?? m.content ?? ''),
          time: String(m.ts ?? m.timestamp ?? ''),
        }))
        // Only seed if live frames haven't already populated the thread.
        setMessages((prev) => (prev.length === 0 ? hist : prev))
        historyLoaded.current = true
      } catch {
        /* history is best-effort */
      }
    },
    [],
  )

  useEffect(() => {
    // Callers mount this in a component keyed by agentId, so the hook
    // instance (and its refs) is already fresh per conversation.
    if (!agentId) return
    let disposed = false

    const connect = () => {
      if (disposed) return
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
      let url = `${proto}//${location.host}/ws/chat/${encodeURIComponent(agentId)}`
      if (lastSeq.current >= 0) url += `?since_seq=${lastSeq.current}`
      setStatus(attempts.current === 0 ? 'connecting' : 'reconnecting')

      const ws = new WebSocket(url)
      wsRef.current = ws

      ws.addEventListener('open', () => {
        ws.send(JSON.stringify({ token: getToken() }))
        attempts.current = 0
        deadline.current = 0
      })

      ws.addEventListener('message', (ev) => {
        let frame: Dict
        try {
          frame = JSON.parse(ev.data as string)
        } catch {
          return
        }

        // Seq-gap detection (SPEC-025 Track A).
        if (typeof frame.seq === 'number') {
          const expected = lastSeq.current + 1
          if (lastSeq.current >= 0 && frame.seq !== expected) {
            try {
              ws.close(4000, 'seq-gap')
            } catch {
              /* noop */
            }
            return
          }
          lastSeq.current = frame.seq
        } else if (typeof frame.lost_below_seq === 'number') {
          lastSeq.current = frame.lost_below_seq - 1
        }

        if (frame.type === 'ready') {
          chatId.current = (frame.chat_id as string) ?? null
          setStatus('ready')
          if (chatId.current) loadHistory(agentId, chatId.current)
          return
        }
        if (frame.error) {
          append({ id: `err${Date.now()}`, role: 'system', text: `Error: ${frame.error}`, time: now() })
          return
        }
        if (frame.type === 'tool_call') {
          append({
            id: `tool${frame.turn_id ?? Date.now()}`,
            role: 'tool_call',
            tool: String(frame.tool ?? 'tool'),
            text: String(frame.args ?? ''),
            time: String(frame.ts ?? now()),
          })
          return
        }
        if (frame.type === 'message' && frame.from === 'agent') {
          const text = String(frame.text ?? '')
          if (text.trim() === '...') return // typing placeholder
          append({ id: `a${frame.seq ?? Date.now()}`, role: 'agent', text, time: now() })
        }
      })

      ws.addEventListener('close', () => {
        wsRef.current = null
        if (disposed || agentId == null) return
        if (deadline.current === 0) deadline.current = Date.now() + RECONNECT_MAX_WINDOW_MS
        if (Date.now() > deadline.current) {
          setStatus('closed')
          append({ id: `sys${Date.now()}`, role: 'system', text: '(could not reconnect — refresh the page)', time: now() })
          return
        }
        setStatus('reconnecting')
        const delay = Math.min(BASE_DELAY * 2 ** attempts.current + Math.random() * 500, MAX_DELAY)
        attempts.current += 1
        reconnectTimer.current = setTimeout(connect, delay)
      })

      ws.addEventListener('error', () => {
        /* close handler reconnects */
      })
    }

    connect()

    return () => {
      disposed = true
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
      try {
        wsRef.current?.close()
      } catch {
        /* noop */
      }
      wsRef.current = null
    }
  }, [agentId, append, loadHistory])

  const sendMessage = useCallback(
    (text: string) => {
      const ws = wsRef.current
      if (!ws || ws.readyState !== WebSocket.OPEN || !text.trim()) return
      clientSeq.current += 1
      append({ id: `u${clientSeq.current}`, role: 'user', text, time: now() })
      ws.send(JSON.stringify({ type: 'message', text, client_seq: clientSeq.current }))
    },
    [append],
  )

  return { messages, status, sendMessage }
}
