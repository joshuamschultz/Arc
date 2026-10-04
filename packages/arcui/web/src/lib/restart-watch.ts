/** Wait for Arc to come back after a restart.
 *
 *  `/api/health` needs no sign-in, so it answers as soon as the server is up. A
 *  restart does not take Arc down at the very instant it is asked for, so an
 *  "ok" is only believed once Arc has been seen down, or once `graceMs` has
 *  passed with no sign of a restart. Resolves `true` when Arc is back, `false`
 *  if `maxMs` runs out first or the caller aborts. */
export interface WatchOptions {
  /** How often to ask. */
  intervalMs?: number
  /** After this long, an answering server counts as "back" even if it never went down. */
  graceMs?: number
  /** Give up after this long. */
  maxMs?: number
}

export const DEFAULT_WATCH: Required<WatchOptions> = {
  intervalMs: 2000,
  graceMs: 8000,
  maxMs: 180_000,
}

export async function isArcUp(): Promise<boolean> {
  try {
    const res = await fetch('/api/health', { cache: 'no-store' })
    return res.ok
  } catch {
    return false
  }
}

export async function waitForArcBack(
  options: WatchOptions = {},
  signal?: AbortSignal,
  probe: () => Promise<boolean> = isArcUp,
): Promise<boolean> {
  const { intervalMs, graceMs, maxMs } = { ...DEFAULT_WATCH, ...options }
  const started = Date.now()
  let sawDown = false
  while (Date.now() - started <= maxMs) {
    if (signal?.aborted) return false
    const up = await probe()
    if (!up) sawDown = true
    if (up && (sawDown || Date.now() - started >= graceMs)) return true
    await new Promise((resolve) => setTimeout(resolve, intervalMs))
  }
  return false
}
