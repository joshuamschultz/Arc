import { useEffect, useLayoutEffect, useRef } from 'react'
import { arcSocket } from '@/lib/arc-socket'
import type { FileChangeMessage } from '@/lib/types'

/** Connect the singleton `/ws` socket once for the app lifetime. */
export function useArcSocketConnect(): void {
  useEffect(() => {
    arcSocket.connect()
  }, [])
}

/**
 * Subscribe to workspace file-change events. Pages use this to refetch on
 * mutation — the storage-agnostic seam: a future DB-backed "row changed"
 * signal swaps in behind the same callback (plan §storage-evolution).
 */
export function useFileChange(fn: (msg: FileChangeMessage) => void): void {
  // Keep the latest callback in a ref so callers can pass an inline function
  // without resubscribing every render (the useEvent pattern).
  const ref = useRef(fn)
  useLayoutEffect(() => {
    ref.current = fn
  })
  useEffect(() => arcSocket.onFileChange((msg) => ref.current(msg)), [])
}

/** Mark one agent active for the lifetime of a detail view (SPEC-022). */
export function useActiveAgent(agentId: string | null): void {
  useEffect(() => {
    arcSocket.setActiveAgent(agentId)
    return () => arcSocket.setActiveAgent(null)
  }, [agentId])
}

/** Fleet pages: keep the live subscription roster in sync with visible agents. */
export function useRosterSubscription(agentIds: string[]): void {
  // Join into a stable key so we don't resubscribe on every array identity.
  const key = agentIds.join(',')
  useEffect(() => {
    arcSocket.subscribeRoster(key ? key.split(',') : [])
  }, [key])
}
