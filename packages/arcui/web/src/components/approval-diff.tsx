import type { ApprovalDiffData } from '@/lib/queries'

const NODE_GROUPS = [
  ['added', 'Nodes added', 'text-status-online'],
  ['removed', 'Nodes removed', 'text-status-error'],
  ['changed', 'Nodes changed', 'text-status-warning'],
] as const

/** Expandable node-level and per-file diff for a workflow approval. */
export function ApprovalDiff({ diff }: { diff: ApprovalDiffData }) {
  const groups = NODE_GROUPS.filter(([key]) => (diff.nodes?.[key] ?? []).length > 0)
  const files = diff.files ?? []
  if (groups.length === 0 && files.length === 0) return null
  return (
    <details className="rounded-lg border border-border bg-muted/20 px-3 py-2">
      <summary className="cursor-pointer text-xs font-medium text-foreground">
        What changes
      </summary>
      <div className="mt-2 space-y-2 text-xs">
        {groups.map(([key, label, tone]) => (
          <div key={key}>
            <span className={`font-semibold ${tone}`}>{label}</span>
            <span className="ml-1.5 font-mono text-foreground/80">
              {(diff.nodes?.[key] ?? []).join(', ')}
            </span>
          </div>
        ))}
        {files.map((f) => (
          <details key={f.path}>
            <summary className="cursor-pointer font-mono text-foreground/90">
              {f.path} <span className="text-muted-foreground">({f.status})</span>
            </summary>
            <pre className="mt-1 max-h-60 overflow-auto rounded bg-muted/30 p-2 text-[11px]">
              {f.diff}
            </pre>
          </details>
        ))}
      </div>
    </details>
  )
}
