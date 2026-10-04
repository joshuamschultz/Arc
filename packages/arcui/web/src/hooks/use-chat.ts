import { useCallback, useEffect, useRef, useState } from 'react'
import { apiGet } from '@/lib/api'
import { getToken } from '@/lib/auth'
import { checkForNewBuild } from '@/lib/stale-build'
import { newRequestId } from '@/lib/request-id'
import type { Dict, SessionReplayResponse } from '@/lib/types'

export type ChatRole = 'user' | 'agent' | 'tool_call' | 'system'

export interface ChatMessage {
  id: string
  role: ChatRole
  text: string
  tool?: string
  time: string
  attachments?: string[]
  streaming?: boolean
}

export type ChatStatus = 'connecting' | 'ready' | 'reconnecting' | 'closed'

const RECONNECT_MAX_WINDOW_MS = 60_000
const BASE_DELAY = 800
const MAX_DELAY = 15_000
const WORKING_POLL_MS = 5_000

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
  const [sessionKey, setSessionKey] = useState<string | null>(null)
  // A run for this session is in flight: from the server on every history load
  // (so a return mid-run shows it), and from the live frames while connected.
  const [working, setWorking] = useState(false)

  const wsRef = useRef<WebSocket | null>(null)
  const lastSeq = useRef(-1)
  const clientSeq = useRef(0)
  const pending = useRef(new Map<string, { text: string; attachmentIds: string[] }>())
  const chatId = useRef<string | null>(null)
  const attempts = useRef(0)
  const deadline = useRef(0)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const historyLoaded = useRef(false)
  // True once a socket dropped: the next `ready` is a return, and the run may
  // have finished while the tab was away, so the session log is the truth.
  const returning = useRef(false)

  const append = useCallback((m: ChatMessage) => {
    setMessages((prev) => [...prev, m])
  }, [])

  const loadHistory = useCallback(
    async (agent: string, sid: string, refresh = false) => {
      if (historyLoaded.current && !refresh) return
      try {
        // tail=1: the NEWEST 200 turns. Page 1 is the oldest slice, which froze
        // long conversations at their beginning and made recent messages look lost.
        const data = await apiGet<SessionReplayResponse>(
          `/api/agents/${agent}/sessions/${sid}?page_size=200&tail=1`,
        )
        if (historyLoaded.current && !refresh) return
        // The session log interleaves real chat turns (role=user/assistant with
        // content) with run-completion telemetry records (type/completion_payload,
        // no role, no text). Keep only chat turns with renderable text so the
        // telemetry rows don't render as empty bubbles.
        const hist: ChatMessage[] = data.messages
          .map((m: Dict, i) => {
            const rawRole = String(m.role ?? m.from ?? '')
            const role: ChatRole = rawRole === 'user' ? 'user' : 'agent'
            return {
              id: `h${i}`,
              role,
              rawRole,
              text: String(m.text ?? m.content ?? '').trim(),
              time: String(m.ts ?? m.timestamp ?? ''),
            }
          })
          .filter(
            (m) => m.text !== '' && (m.rawRole === 'user' || m.rawRole === 'assistant'),
          )
          .map(({ rawRole: _rawRole, ...m }) => m)
        // First load: only seed if live frames haven't populated the thread.
        // Return after a drop: a run never stops because the tab left, so the
        // log holds answers finished while away. Replace the thread with it,
        // unless a reply is streaming live or a sent message is not yet logged.
        setMessages((prev) => {
          if (!refresh) return prev.length === 0 ? hist : prev
          const liveWork = pending.current.size > 0 || prev.some((m) => m.streaming)
          return liveWork ? prev : hist
        })
        setWorking(Boolean(data.run_in_flight) || pending.current.size > 0)
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
        // A reconnect after a deploy lands on the new server; see if this tab is old.
        if (attempts.current > 0) void checkForNewBuild()
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
          setSessionKey(chatId.current)
          setStatus('ready')
          if (chatId.current) {
            loadHistory(agentId, chatId.current, returning.current)
            returning.current = false
          }
          for (const [requestId, message] of pending.current) {
            clientSeq.current += 1
            ws.send(JSON.stringify({
              type: 'message', text: message.text, client_seq: clientSeq.current,
              attachment_ids: message.attachmentIds, request_id: requestId,
            }))
          }
          return
        }
        if (frame.error) {
          append({ id: `err${Date.now()}`, role: 'system', text: `Error: ${frame.error}`, time: now() })
          return
        }
        if (frame.type === 'error') {
          // A typed refusal ({type:'error', code, message}) — a rejected frame
          // must never look like a message that is still being worked on.
          const reason = String(frame.message ?? frame.code ?? 'the server refused the message')
          append({ id: `err${Date.now()}`, role: 'system', text: `Error: ${reason}`, time: now() })
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
        if (frame.type === 'stream') {
          const runId = String(frame.run_id ?? '')
          if (!runId) return
          setWorking(frame.event !== 'end')
          if (frame.event === 'tool') {
            append({
              id: `tool-${runId}-${frame.event_sequence ?? frame.seq ?? Date.now()}`,
              role: 'tool_call',
              tool: String(frame.tool ?? 'tool'),
              text: '',
              time: String(frame.ts ?? now()),
            })
            return
          }
          if (frame.event === 'text') {
            const text = String(frame.text ?? '')
            if (!text) return
            setMessages((previous) => {
              const id = `stream-${runId}`
              const index = previous.findIndex((message) => message.id === id)
              if (index < 0) {
                return [...previous, { id, role: 'agent', text, time: String(frame.ts ?? now()), streaming: true }]
              }
              const next = [...previous]
              const current = next[index]
              next[index] = { ...current, text: current.text + text, streaming: true }
              return next
            })
            return
          }
          if (frame.event === 'end') {
            if (typeof frame.request_id === 'string') pending.current.delete(frame.request_id)
            if (typeof frame.status === 'string' && frame.status !== 'completed') {
              const reason = typeof frame.reason === 'string' && frame.reason ? ` ${frame.reason}` : ''
              append({ id: `end-${runId}`, role: 'system', text: `The run ${frame.status.replace('_', ' ')}.${reason}`, time: now() })
            }
            setMessages((previous) =>
              previous.map((message) =>
                message.id === `stream-${runId}` ? { ...message, streaming: false } : message,
              ),
            )
          }
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
        returning.current = true
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

  // While a run is in flight, re-read the log so the answer lands even if the
  // live frames never reach this tab (it was away when the run started).
  useEffect(() => {
    if (!working || status !== 'ready' || !agentId || !sessionKey) return
    const timer = setInterval(() => {
      void loadHistory(agentId, sessionKey, true)
    }, WORKING_POLL_MS)
    return () => clearInterval(timer)
  }, [working, status, agentId, sessionKey, loadHistory])

  const resetForNewSession = useCallback(() => {
    // The backend has already rotated the session key; drop the current thread
    // and force a reconnect through the existing close→reconnect path. The new
    // socket's `ready` frame carries the rotated chat_id, and with lastSeq reset
    // the URL omits `since_seq` so the seq-gap detector restarts clean. Marking
    // history as loaded short-circuits the preload — the new session is empty.
    setMessages([])
    lastSeq.current = -1
    chatId.current = null
    setSessionKey(null)
    historyLoaded.current = true
    pending.current.clear()
    setWorking(false)
    try {
      wsRef.current?.close()
    } catch {
      /* close handler reconnects */
    }
  }, [])

  const sendMessage = useCallback(
    (text: string, attachmentIds: string[] = []) => {
      const opaqueIds = attachmentIds.filter((id) => /^att_[A-Za-z0-9_-]+$/.test(id))
      if (!text.trim() && opaqueIds.length === 0) return false
      const ws = wsRef.current
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        // Never drop a message silently. If the socket isn't open (the
        // backend died, or we're mid-reconnect) the user gets explicit
        // feedback instead of a typed message that vanishes with no reply,
        // no LLM call, and no run.
        append({
          id: `sys-nosend-${Date.now()}`,
          role: 'system',
          text: 'Not connected — message not sent. Waiting to reconnect to the backend…',
          time: now(),
        })
        return false
      }
      const requestId = newRequestId()
      pending.current.set(requestId, { text, attachmentIds: opaqueIds })
      setWorking(true)
      clientSeq.current += 1
      append({
        id: `u${clientSeq.current}`,
        role: 'user',
        text: text || `Sent ${opaqueIds.length} attachment${opaqueIds.length === 1 ? '' : 's'}`,
        time: now(),
        attachments: opaqueIds,
      })
      ws.send(JSON.stringify({ type: 'message', text, client_seq: clientSeq.current, attachment_ids: opaqueIds, request_id: requestId }))
      return true
    },
    [append],
  )

  return { messages, status, sessionKey, working, sendMessage, resetForNewSession }
}
