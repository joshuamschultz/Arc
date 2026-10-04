/** The run detail page for one workflow run. */
export function runPath(workflowId: string, runId: string): string {
  return `/workflows/${encodeURIComponent(workflowId)}/runs/${encodeURIComponent(runId)}`
}
