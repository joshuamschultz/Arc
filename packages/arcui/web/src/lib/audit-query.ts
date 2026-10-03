// The query string for GET /api/team/audit — kept in lib/ so queries.ts never
// reaches up into components/.

export type AuditFilters = Partial<
  Record<
    | 'filter'
    | 'agent'
    | 'initiator'
    | 'run_id'
    | 'tool_call_id'
    | 'llm_call_id'
    | 'workflow_run_id'
    | 'node_id'
    | 'task_id'
    | 'connection_id',
    string
  >
>

/** `limit=N&key=value…` for GET /api/team/audit; blank filters are left out. */
export function auditQuery(filters: AuditFilters, limit: number): string {
  const params = [`limit=${limit}`]
  for (const [key, value] of Object.entries(filters)) {
    if (value) params.push(`${key}=${encodeURIComponent(value)}`)
  }
  return params.join('&')
}
