import { create } from 'zustand'

/** Mirrors the four WS_STATES the old `ws-client.js` exposed. */
export type ConnectionStatus =
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'disconnected'

/** Role granted by the server's `auth_ok` frame. Gates operator-only UI. */
export type Role = 'viewer' | 'operator' | 'agent' | null

interface ConnectionState {
  status: ConnectionStatus
  role: Role
  /** ms epoch of the last inbound frame, for stale detection. */
  lastMessageAt: number | null
  /** True after an `auth_ok`; false after an auth error frame. */
  authError: boolean
  setStatus: (status: ConnectionStatus) => void
  setRole: (role: Role) => void
  setAuthError: (authError: boolean) => void
  markMessage: () => void
}

/**
 * Live WebSocket connection status. Phase 2's `useRobustWebSocket` hook
 * drives `setStatus`/`markMessage`; the topbar pill and connection banner
 * read from here.
 */
export const useConnectionStore = create<ConnectionState>((set) => ({
  status: 'connecting',
  role: null,
  lastMessageAt: null,
  authError: false,
  setStatus: (status) => set({ status }),
  setRole: (role) => set({ role }),
  setAuthError: (authError) => set({ authError }),
  markMessage: () => set({ lastMessageAt: Date.now() }),
}))
