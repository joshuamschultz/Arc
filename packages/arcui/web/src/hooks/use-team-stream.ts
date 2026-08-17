import { useCallback, useEffect, useRef, useState } from 'react'
import { getToken } from '@/lib/auth'

export interface TeamFrame {
  type: string
  channel: string
  from: string
  body: string
  mentions: string[]
  seq: number
  ts: string
  id: string
  action_required?: boolean
  // SPEC-061 ArcFlow (COMP-010 narration, not yet merged — reconciliation
  // note): present on a `team_message` frame that narrates a workflow gate
  // node waiting for a human decision. Keys the in-channel gate card
  // (`components/gate-card.tsx`) to `POST /api/workflow-tasks/{task_id}/gate`
  // (COMP-018) — never a status flip on the generic task approve/reject.
  gate?: {
    task_id: string
    workflow_id?: string
    node_id?: string
  }
}

export type TeamStreamStatus = 'connecting' | 'ready' | 'closed'

/** How long to wait for the server's `posted` acknowledgement before failing. */
const POST_ACK_TIMEOUT_MS = 10_000

/** Human-readable text for the error codes `routes/team_ws.py` can send. */
const POST_ERRORS: Record<string, string> = {
  forward_unavailable: 'Team posting is not configured on this server.',
  forward_failed: 'The server could not deliver your message to the team.',
  missing_channel: 'No channel selected.',
  empty: 'Message was empty.',
  malformed: 'The server could not read that message.',
}

interface PendingPost {
  resolve: () => void
  reject: (err: Error) => void
  timer: ReturnType<typeof setTimeout>
}

/**
 * Live stream of one team channel over `/ws/team` (SPEC-031 F1/F2).
 *
 * The socket is a thin view: it renders flows the server pushes and forwards
 * human posts back through `post()`. It never signs or routes — the server
 * (arcteam) owns that.
 *
 * `post()` resolves only when the server acknowledges with `posted`, and
 * rejects on any error frame, on a closed socket, or on timeout. SPEC-068 F3:
 * the previous version parsed every error frame and dropped it, so a post that
 * never reached arcteam looked identical to one that did.
 */
export function useTeamStream(channel: string | null) {
  const [frames, setFrames] = useState<TeamFrame[]>([])
  const [status, setStatus] = useState<TeamStreamStatus>('connecting')
  const wsRef = useRef<WebSocket | null>(null)
  const pendingRef = useRef<PendingPost | null>(null)

  // Settle whatever post is in flight. Every exit path routes through here so a
  // pending promise can never outlive its socket.
  const settle = useCallback((err: Error | null) => {
    const pending = pendingRef.current
    if (!pending) return
    pendingRef.current = null
    clearTimeout(pending.timer)
    if (err) pending.reject(err)
    else pending.resolve()
  }, [])

  useEffect(() => {
    // ChannelPanel is keyed by channel, so this hook remounts per channel and
    // starts from fresh `[]` / 'connecting' state — no in-effect reset needed
    // (which would trip react-hooks/set-state-in-effect).
    if (!channel) return
    let disposed = false

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${location.host}/ws/team?channel=${encodeURIComponent(channel)}`
    const ws = new WebSocket(url)
    wsRef.current = ws

    ws.addEventListener('open', () => {
      ws.send(JSON.stringify({ token: getToken() }))
    })

    ws.addEventListener('message', (ev) => {
      let frame: TeamFrame & { code?: string; message?: string; error?: string }
      try {
        frame = JSON.parse(ev.data as string)
      } catch {
        return
      }
      if (frame.type === 'ready') {
        setStatus('ready')
        return
      }
      if (frame.type === 'posted') {
        settle(null)
        return
      }
      if (frame.type === 'error') {
        const code = frame.code ?? ''
        settle(new Error(POST_ERRORS[code] ?? frame.message ?? 'Could not send message.'))
        return
      }
      // A bare `{"error": ...}` is the auth / stream-disabled refusal shape.
      if (frame.error) {
        settle(new Error(frame.error))
        return
      }
      if (frame.type === 'team_message') {
        setFrames((prev) =>
          frame.id && prev.some((f) => f.id === frame.id) ? prev : [...prev, frame],
        )
      }
    })

    ws.addEventListener('close', () => {
      wsRef.current = null
      settle(new Error('Connection to the team stream closed.'))
      if (!disposed) setStatus('closed')
    })

    return () => {
      disposed = true
      settle(new Error('Channel closed.'))
      try {
        ws.close()
      } catch {
        /* noop */
      }
      wsRef.current = null
    }
  }, [channel, settle])

  /**
   * Send one post and wait for the server's acknowledgement.
   *
   * Rejects rather than returning silently: a caller must not clear the compose
   * box until this resolves.
   */
  const post = useCallback(
    (text: string): Promise<void> => {
      const ws = wsRef.current
      if (!channel) return Promise.reject(new Error('No channel selected.'))
      if (!text.trim()) return Promise.reject(new Error('Message was empty.'))
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        return Promise.reject(new Error('Not connected to the team stream.'))
      }
      if (pendingRef.current) {
        return Promise.reject(new Error('Still sending the previous message.'))
      }
      return new Promise<void>((resolve, reject) => {
        const timer = setTimeout(
          () => settle(new Error('The server did not acknowledge the message.')),
          POST_ACK_TIMEOUT_MS,
        )
        pendingRef.current = { resolve, reject, timer }
        try {
          ws.send(JSON.stringify({ type: 'post', channel, text }))
        } catch {
          settle(new Error('Could not send message.'))
        }
      })
    },
    [channel, settle],
  )

  return { frames, status, post }
}
