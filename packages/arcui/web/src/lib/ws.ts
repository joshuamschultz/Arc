// TypeScript port of the old `ws-client.js` RobustWebSocket: exponential
// backoff + jitter, 30s heartbeat, first-message token auth, and an outbox
// that buffers sends while disconnected. Framework-agnostic on purpose —
// `arc-socket.ts` wraps it with the app-specific routing.

export const WS_STATES = {
  CONNECTING: 0,
  CONNECTED: 1,
  RECONNECTING: 2,
  DISCONNECTED: 3,
} as const

export type WsState = (typeof WS_STATES)[keyof typeof WS_STATES]

interface RobustWebSocketOpts {
  maxRetries?: number
  baseDelay?: number
  maxDelay?: number
  heartbeatInterval?: number
}

type StateListener = (state: WsState) => void
type MessageListener = (data: Record<string, unknown>) => void

export class RobustWebSocket {
  private maxRetries: number
  private baseDelay: number
  private maxDelay: number
  private heartbeatInterval: number
  private retries = 0
  private ws: WebSocket | null = null
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null
  private _state: WsState = WS_STATES.DISCONNECTED
  private outbox: unknown[] = []
  private stateListeners = new Set<StateListener>()
  private messageListeners = new Set<MessageListener>()
  private url: string
  private token: string

  constructor(url: string, token: string, opts: RobustWebSocketOpts = {}) {
    this.url = url
    this.token = token
    this.maxRetries = opts.maxRetries ?? 10
    this.baseDelay = opts.baseDelay ?? 1000
    this.maxDelay = opts.maxDelay ?? 30000
    this.heartbeatInterval = opts.heartbeatInterval ?? 30000
  }

  get state(): WsState {
    return this._state
  }

  onState(fn: StateListener): () => void {
    this.stateListeners.add(fn)
    return () => this.stateListeners.delete(fn)
  }

  onMessage(fn: MessageListener): () => void {
    this.messageListeners.add(fn)
    return () => this.messageListeners.delete(fn)
  }

  connect(): void {
    this.setState(WS_STATES.CONNECTING)
    try {
      this.ws = new WebSocket(this.url)
    } catch {
      this.scheduleReconnect()
      return
    }

    this.ws.onopen = () => {
      // First-message auth.
      this.ws?.send(JSON.stringify({ token: this.token }))
    }

    this.ws.onmessage = (evt) => {
      let data: Record<string, unknown>
      try {
        data = JSON.parse(evt.data as string)
      } catch {
        return
      }

      if (data.type === 'auth_ok') {
        this.setState(WS_STATES.CONNECTED)
        this.retries = 0
        this.startHeartbeat()
        this.flushOutbox()
        this.emit(data) // let listeners capture role
        return
      }
      if (data.error) {
        this.setState(WS_STATES.DISCONNECTED)
        this.ws?.close()
        this.emit(data)
        return
      }
      if (data.type === 'ping') {
        this.ws?.send(JSON.stringify({ type: 'pong' }))
        return
      }
      this.emit(data)
    }

    this.ws.onclose = () => {
      this.stopHeartbeat()
      if (this._state !== WS_STATES.DISCONNECTED) this.scheduleReconnect()
    }

    this.ws.onerror = () => {
      /* onclose fires next */
    }
  }

  send(data: unknown): void {
    if (this._state === WS_STATES.CONNECTED && this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(typeof data === 'string' ? data : JSON.stringify(data))
    } else {
      this.outbox.push(data)
    }
  }

  /** Update the token used on the next (re)connect, e.g. after sign-in. */
  setToken(token: string): void {
    this.token = token
  }

  disconnect(): void {
    this.setState(WS_STATES.DISCONNECTED)
    this.stopHeartbeat()
    if (this.ws) {
      this.ws.close()
      this.ws = null
    }
  }

  private emit(data: Record<string, unknown>): void {
    for (const fn of this.messageListeners) fn(data)
  }

  private setState(s: WsState): void {
    if (this._state === s) return
    this._state = s
    for (const fn of this.stateListeners) fn(s)
  }

  private scheduleReconnect(): void {
    if (this.retries >= this.maxRetries) {
      this.setState(WS_STATES.DISCONNECTED)
      return
    }
    this.setState(WS_STATES.RECONNECTING)
    const delay = Math.min(
      this.baseDelay * Math.pow(2, this.retries) + Math.random() * 1000,
      this.maxDelay,
    )
    this.retries++
    setTimeout(() => this.connect(), delay)
  }

  private startHeartbeat(): void {
    this.stopHeartbeat()
    this.heartbeatTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ type: 'pong' }))
      }
    }, this.heartbeatInterval)
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer)
      this.heartbeatTimer = null
    }
  }

  private flushOutbox(): void {
    while (this.outbox.length > 0) {
      this.send(this.outbox.shift())
    }
  }
}
