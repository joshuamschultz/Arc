import { RefreshCw } from 'lucide-react'
import { useBuildIsStale } from '@/lib/stale-build'

/**
 * Persistent notice that the server is running a newer build than this tab.
 *
 * Deliberately not dismissable: the tab's JavaScript is out of date, so actions
 * can silently misbehave until it is reloaded. It sits in the page flow (sticky at
 * the top of the content column) so it never overlays the navigation on a phone.
 */
export function NewBuildBanner() {
  if (!useBuildIsStale()) return null
  return (
    <div
      role="alert"
      className="sticky top-0 z-30 flex flex-wrap items-center justify-between gap-2 border-b border-primary/40 bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
    >
      <span>Arc was updated. Reload to use the new version.</span>
      <button
        type="button"
        onClick={() => window.location.reload()}
        className="inline-flex min-h-9 items-center gap-1.5 rounded-[10px] bg-primary-foreground px-3 text-primary outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <RefreshCw className="size-4" aria-hidden />
        Reload
      </button>
    </div>
  )
}
