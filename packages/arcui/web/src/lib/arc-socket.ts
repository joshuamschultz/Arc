// Singleton bridge between the `/ws` RobustWebSocket and the app: it routes
// inbound frames to the connection + live stores, runs the SPEC-022 subscribe
// protocol (`subscribe:agent` / `file_change`), and exposes a file-change bus
// so pages can refetch on workspace mutations. One socket per tab.

import { RobustWebSocket, WS_STATES, type WsState } from './ws'
import { getToken } from './auth'
import { useConnectionStore, type Role, type ConnectionStatus } from '@/store/connection'
import { useLiveStore } from '@/store/live'
import type { FileChangeMessage, UIEvent } from './types'

const STATE_TO_STATUS: Record<WsState, ConnectionStatus> = {
  [WS_STATES.CONNECTING]: 'connecting',
  [WS_STATES.CONNECTED]: 'connected',
  [WS_STATES.RECONNECTING]: 'reconnecting',
  [WS_STATES.DISCONNECTED]: 'disconnected',
}

type FileChangeListener = (msg: FileChangeMessage) => void

class ArcSocket {
  private ws: RobustWebSocket | null = null
  private subs = new Set<string>()
  private activeAgent: string | null = null
  private roster = new Set<string>()
  private fileChangeListeners = new Set<FileChangeListener>()

  /** Idempotent — connects the singleton socket once. */
  connect(): void {
    if (this.ws) return
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    this.ws = new RobustWebSocket(`${proto}//${location.host}/ws`, getToken())

    this.ws.onState((state) => {
      useConnectionStore.getState().setStatus(STATE_TO_STATUS[state])
      // Re-fire subscriptions after a reconnect (server forgot them).
      if (state === WS_STATES.CONNECTED) this.resubscribe()
    })

    this.ws.onMessage((data) => {
      useConnectionStore.getState().markMessage()
      this.route(data)
    })

    this.ws.connect()
  }

  private route(data: Record<string, unknown>): void {
    if (data.type === 'auth_ok') {
      const conn = useConnectionStore.getState()
      conn.setRole((data.role as Role) ?? 'viewer')
      conn.setAuthError(false)
      return
    }
    if (data.error) {
      useConnectionStore.getState().setAuthError(true)
      return
    }
    if (data.type === 'file_change') {
      const msg = data as unknown as FileChangeMessage
      for (const fn of this.fileChangeListeners) fn(msg)
      return
    }
    if (data.type === 'event_batch') {
      useLiveStore.getState().handleEventBatch(data as { events?: UIEvent[] })
      return
    }
    // Bare UIEvent (SubscriptionManager.broadcast_filtered) — wrap as a batch.
    if (data.layer && data.event_type) {
      useLiveStore.getState().handleEventBatch({ events: [data as unknown as UIEvent] })
    }
  }

  // --- SPEC-022 subscribe protocol ----------------------------------------

  subscribeAgent(agentId: string): void {
    if (!agentId || this.subs.has(agentId)) return
    this.subs.add(agentId)
    this.ws?.send({ type: 'subscribe:agent', agent_id: agentId })
  }

  unsubscribeAgent(agentId: string): void {
    if (!agentId || !this.subs.has(agentId)) return
    this.subs.delete(agentId)
    this.ws?.send({ type: 'unsubscribe:agent', agent_id: agentId })
  }

  /** Route-change convenience: drop the previous active agent, sub the new. */
  setActiveAgent(agentId: string | null): void {
    if (this.activeAgent === agentId) return
    if (this.activeAgent && !this.roster.has(this.activeAgent)) {
      this.unsubscribeAgent(this.activeAgent)
    }
    this.activeAgent = agentId
    if (agentId) this.subscribeAgent(agentId)
  }

  /** Fleet pages subscribe to every agent at once. */
  subscribeRoster(agentIds: string[]): void {
    const next = new Set(agentIds)
    for (const id of this.roster) {
      if (!next.has(id) && id !== this.activeAgent) this.unsubscribeAgent(id)
    }
    for (const id of next) this.subscribeAgent(id)
    this.roster = next
  }

  private resubscribe(): void {
    const ids = Array.from(this.subs)
    this.subs.clear()
    for (const id of ids) this.subscribeAgent(id)
  }

  onFileChange(fn: FileChangeListener): () => void {
    this.fileChangeListeners.add(fn)
    return () => this.fileChangeListeners.delete(fn)
  }

  /** Reconnect with a freshly entered token (after sign-in). */
  reauth(token: string): void {
    this.ws?.setToken(token)
    this.ws?.disconnect()
    this.ws = null
    useConnectionStore.getState().setAuthError(false)
    this.connect()
  }
}

export const arcSocket = new ArcSocket()
