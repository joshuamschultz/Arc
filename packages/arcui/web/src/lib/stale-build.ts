/**
 * Detect that this tab is running code the server no longer has, and reload once.
 *
 * Asset filenames are content-hashed and a deploy deletes the previous ones, so a
 * tab left open across one breaks in two ways that look unrelated: a lazily-imported
 * route asks for a chunk that is gone ("Importing a module script failed"), and old
 * components render new API shapes ("objects are not valid as a React child"). Both
 * happened on a single deploy.
 *
 * No cache header fixes this. The page is already in memory and never asks for HTML
 * again, so it cannot learn it is stale — it has to check.
 */

const CHECK_KEY = 'arcui:stale-reloaded'

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

/**
 * Reload when the deployed bundle differs from this tab's.
 *
 * Reloads at most once per tab: if the page comes back still mismatched, the fault
 * is not staleness and a reload loop would hide it behind a flickering screen. An
 * unreachable or unversioned server is treated as "no information", never as stale —
 * a network blip must not throw away unsaved state.
 */
export async function reloadIfStale(): Promise<void> {
  if (sessionStorage.getItem(CHECK_KEY)) return
  const mine = ownBundle()
  if (!mine) return
  let deployed: string
  try {
    deployed = await deployedBundle()
  } catch {
    return
  }
  if (!deployed || deployed === mine) return
  sessionStorage.setItem(CHECK_KEY, '1')
  window.location.reload()
}

/**
 * Check on load and whenever the tab is refocused.
 *
 * Focus is the moment that matters: the tab someone left open over a deploy is
 * exactly the tab they come back to and click something in.
 */
export function watchForStaleBuild(): () => void {
  void reloadIfStale()
  const onFocus = () => void reloadIfStale()
  window.addEventListener('focus', onFocus)
  return () => window.removeEventListener('focus', onFocus)
}
