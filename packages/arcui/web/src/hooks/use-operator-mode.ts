import { useSyncExternalStore } from 'react'

// arcui's auth layer does not expose the caller's role to the frontend today
// (no `/api/me`, no role frame over the WS handshakes) — see settings.tsx's
// `editable = false`. Every mutation control below defaults OFF (safe/viewer
// assumption); this is a manual, explicit, session-local opt-in for someone
// who holds the operator token, not a role determination. The server is the
// real gate: every PATCH/DELETE/PUT still enforces the operator role and a
// viewer token gets a verbatim 403 regardless of this toggle.

const KEY = 'arcui_operator_mode'
const listeners = new Set<() => void>()

function read(): boolean {
  try {
    return localStorage.getItem(KEY) === '1'
  } catch {
    return false
  }
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb)
  return () => listeners.delete(cb)
}

export function useOperatorMode(): [boolean, (v: boolean) => void] {
  const on = useSyncExternalStore(subscribe, read, () => false)
  const set = (v: boolean) => {
    try {
      localStorage.setItem(KEY, v ? '1' : '0')
    } catch {
      /* localStorage disabled — toggle no-ops, controls stay hidden */
    }
    listeners.forEach((cb) => cb())
  }
  return [on, set]
}
