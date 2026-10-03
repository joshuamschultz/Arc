/**
 * Detect that this tab is running code the server no longer has, and say so.
 *
 * Asset filenames are content-hashed and a deploy deletes the previous ones, so a
 * tab left open across one breaks in ways that look unrelated: a lazily-imported
 * route asks for a chunk that is gone, old components render new API shapes, and an
 * old panel silently never sends what the new server expects. No cache header fixes
 * this. The page is already in memory and never asks for HTML again, so it cannot
 * learn it is stale; it has to check.
 *
 * The result is a banner (never an automatic reload), because a reload would throw
 * away whatever the operator is in the middle of. Once stale, a tab stays stale.
 */
import { useSyncExternalStore } from 'react'

const CHECK_INTERVAL_MS = 60_000

let stale = false
const listeners = new Set<() => void>()

/** This tab's own entry bundle, e.g. `index-BNaLpT5A.js`. */
function ownBundle(): string {
  const scripts = Array.from(document.querySelectorAll('script[src]'))
  for (const script of scripts) {
    const src = script.getAttribute('src') ?? ''
    const match = /\/assets\/(index-[A-Za-z0-9_-]+\.js)/.exec(src)
    if (match) return match[1]
  }
  return ''
}

async function deployedBundle(): Promise<string> {
  const resp = await fetch('/api/health', { cache: 'no-store' })
  if (!resp.ok) return ''
  const body = (await resp.json()) as { bundle?: string }
  return body.bundle ?? ''
}

function markStale(): void {
  if (stale) return
  stale = true
  listeners.forEach((notify) => notify())
}

/**
 * Compare the deployed bundle with this tab's and flag a mismatch.
 *
 * An unreachable or unversioned server is "no information", never "stale": a
 * network blip must not raise a false alarm.
 */
export async function checkForNewBuild(): Promise<void> {
  if (stale) return
  const mine = ownBundle()
  if (!mine) return
  try {
    const deployed = await deployedBundle()
    if (deployed && deployed !== mine) markStale()
  } catch {
    /* unreachable server: no information */
  }
}

/**
 * Check on load, every 60 s, and whenever the tab is refocused. API 5xx and
 * websocket reconnects call `checkForNewBuild` directly.
 */
export function watchForNewBuild(): () => void {
  void checkForNewBuild()
  const onFocus = () => void checkForNewBuild()
  window.addEventListener('focus', onFocus)
  const timer = setInterval(() => void checkForNewBuild(), CHECK_INTERVAL_MS)
  return () => {
    window.removeEventListener('focus', onFocus)
    clearInterval(timer)
  }
}

function subscribe(notify: () => void): () => void {
  listeners.add(notify)
  return () => listeners.delete(notify)
}

export function useBuildIsStale(): boolean {
  return useSyncExternalStore(subscribe, () => stale)
}

/** Test seam: forget a previous test's verdict. */
export function resetBuildWatchForTests(): void {
  stale = false
  listeners.clear()
}
