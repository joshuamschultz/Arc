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
}

export type TeamStreamStatus = 'connecting' | 'ready' | 'closed'

/**
 * Read-only live stream of one team channel over `/ws/team` (SPEC-031 F1).
 *
 * The socket is a thin view: it renders flows the server pushes and forwards
 * human posts back through `post()`. It never signs or routes — the server
 * (arcteam) owns that. Replaces the old 5-second `useChannelMessages` poll.
 */
export function useTeamStream(channel: string | null) {
  const [frames, setFrames] = useState<TeamFrame[]>([])
  const [status, setStatus] = useState<TeamStreamStatus>('connecting')
  const wsRef = useRef<WebSocket | null>(null)

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
      let frame: TeamFrame
      try {
        frame = JSON.parse(ev.data as string)
      } catch {
        return
      }
      if (frame.type === 'ready') {
        setStatus('ready')
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
      if (!disposed) setStatus('closed')
    })

    return () => {
      disposed = true
      try {
        ws.close()
      } catch {
        /* noop */
      }
      wsRef.current = null
    }
  }, [channel])

  const post = useCallback(
    (text: string) => {
      const ws = wsRef.current
      if (!channel || !text.trim() || !ws || ws.readyState !== WebSocket.OPEN) return
      ws.send(JSON.stringify({ type: 'post', channel, text }))
    },
    [channel],
  )

  return { frames, status, post }
}
