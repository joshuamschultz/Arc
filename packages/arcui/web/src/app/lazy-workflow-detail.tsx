import { lazy, Suspense } from 'react'

// SPEC-061 ArcFlow: route-split so `@xyflow/react` + `dagre` only load when a
// workflow detail page is visited — every other dashboard page pays nothing
// for the graph library (DESIGN.md §8 Research Insights). Kept in its own
// file (not router.tsx) so this file exports only a component, satisfying
// react-refresh/only-export-components.
const WorkflowDetailPage = lazy(() =>
  import('@/pages/workflow-detail').then((m) => ({ default: m.WorkflowDetailPage })),
)

export function LazyWorkflowDetailPage() {
  return (
    <Suspense fallback={null}>
      <WorkflowDetailPage />
    </Suspense>
  )
}
