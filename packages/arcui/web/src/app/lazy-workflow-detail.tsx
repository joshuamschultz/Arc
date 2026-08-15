import { lazy, Suspense } from 'react'

// A chunk this page was built against is deleted by the next deploy, so a tab
// left open across one asks for a filename that no longer exists and dies with
// "Importing a module script failed" — an unrecoverable error screen for what is
// really just a stale tab. Cache headers cannot help here: the page is already
// holding the old bundle in memory and never asks the server for HTML again.
//
// So: reload once, which fetches the current index.html and its current chunk
// names. The sentinel makes it strictly once — if the import still fails on the
// freshly loaded page then the fault is real, and a reload loop would hide it
// behind a flickering screen forever. It is cleared on a successful import, so
// the one-shot budget is available again for the NEXT deploy rather than being
// spent for the life of the tab.
const RELOADED_KEY = 'arcui:chunk-reloaded'

function retryAfterDeploy(error: unknown): never {
  if (typeof window !== 'undefined' && !sessionStorage.getItem(RELOADED_KEY)) {
    sessionStorage.setItem(RELOADED_KEY, '1')
    window.location.reload()
  }
  throw error
}

// SPEC-061 ArcFlow: route-split so `@xyflow/react` + `dagre` only load when a
// workflow detail page is visited — every other dashboard page pays nothing
// for the graph library (DESIGN.md §8 Research Insights). Kept in its own
// file (not router.tsx) so this file exports only a component, satisfying
// react-refresh/only-export-components.
const WorkflowDetailPage = lazy(() =>
  import('@/pages/workflow-detail')
    .then((m) => {
      if (typeof window !== 'undefined') sessionStorage.removeItem(RELOADED_KEY)
      return { default: m.WorkflowDetailPage }
    })
    .catch(retryAfterDeploy),
)

export function LazyWorkflowDetailPage() {
  return (
    <Suspense fallback={null}>
      <WorkflowDetailPage />
    </Suspense>
  )
}
