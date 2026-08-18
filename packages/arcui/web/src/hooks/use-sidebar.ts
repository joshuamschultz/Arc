import { useCallback, useEffect, useState } from 'react'

const STORAGE_KEY = 'arc-sidebar-expanded'

function getInitialExpanded(): boolean {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored !== null) return stored === '1'
  } catch {
    // localStorage unavailable (private mode / sandboxed iframe) — use default.
  }
  return false // default: collapsed icon rail
}

/** Persisted expand/collapse state for the primary navigation rail. */
export function useSidebar() {
  const [expanded, setExpanded] = useState(getInitialExpanded)

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, expanded ? '1' : '0')
    } catch {
      // localStorage unavailable — state still applies for this session.
    }
  }, [expanded])

  const toggle = useCallback(() => setExpanded(e => !e), [])

  return { expanded, toggle }
}
