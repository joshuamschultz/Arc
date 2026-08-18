import { useCallback, useSyncExternalStore } from 'react'

// Composer drafts live outside the panel components so they survive the
// key-based remount that happens when the operator switches agents/channels
// (messages.tsx renders each panel with key={sel.id}). Local useState was
// discarded on every switch, so a half-typed message vanished on click-away.
//
// Session-scoped: an in-memory Map is the source of truth; sessionStorage is a
// best-effort mirror so a reload keeps the draft too. Keys are namespaced by
// conversation, e.g. `agent:coder_agent` / `channel:work`.

const PREFIX = 'arcui_draft:'
const drafts = new Map<string, string>()
const listeners = new Set<() => void>()

function read(key: string): string {
  if (drafts.has(key)) return drafts.get(key) ?? ''
  try {
    const stored = sessionStorage.getItem(PREFIX + key)
    if (stored !== null) drafts.set(key, stored)
    return stored ?? ''
  } catch {
    return ''
  }
}

function write(key: string, value: string): void {
  drafts.set(key, value)
  try {
    if (value) sessionStorage.setItem(PREFIX + key, value)
    else sessionStorage.removeItem(PREFIX + key)
  } catch {
    /* sessionStorage disabled — draft still survives in-memory this session */
  }
  listeners.forEach((cb) => cb())
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb)
  return () => listeners.delete(cb)
}

export function useComposerDraft(key: string): [string, (v: string) => void] {
  const value = useSyncExternalStore(
    subscribe,
    () => read(key),
    () => '',
  )
  const set = useCallback((v: string) => write(key, v), [key])
  return [value, set]
}
