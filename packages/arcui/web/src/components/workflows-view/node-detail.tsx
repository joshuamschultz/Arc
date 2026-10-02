import type { WorkflowRunNodeStatus } from '@/lib/types'

/** A bounded wire value: small values arrive as-is, large ones as a marked preview. */
interface TruncatedValue {
  truncated: true
  size_bytes: number
  preview: string
}

function isTruncated(v: unknown): v is TruncatedValue {
  return typeof v === 'object' && v !== null && (v as TruncatedValue).truncated === true
}

function isWithheld(v: unknown): boolean {
  return typeof v === 'object' && v !== null && 'withheld' in (v as object)
}

function IoBlock({ label, value }: { label: string; value: unknown }) {
  if (value === null || value === undefined) return null
  return (
    <details className="mt-1">
      <summary className="cursor-pointer text-[11px] text-muted-foreground">{label}</summary>
      {isWithheld(value) ? (
        <p className="mt-1 text-[11px] text-status-warning">
          Withheld: above this surface's clearance.
        </p>
      ) : isTruncated(value) ? (
        <>
          <p className="mt-1 text-[11px] text-muted-foreground">
            Truncated preview of {value.size_bytes} bytes.
          </p>
          <pre className="mt-1 max-h-48 overflow-auto rounded bg-muted/30 p-2 text-[11px]">
            {value.preview}
          </pre>
        </>
      ) : (
        <pre className="mt-1 max-h-48 overflow-auto rounded bg-muted/30 p-2 text-[11px]">
          {JSON.stringify(value, null, 2)}
        </pre>
      )}
    </details>
  )
}

/** Per-node failure reason, attempt count and bounded input/output for the run feed. */
export function NodeDetail({ node }: { node: WorkflowRunNodeStatus }) {
  const hasAttempts = node.attempts != null && node.max_attempts != null
  const hasIo = node.input != null || node.output != null
  if (!node.last_error && !hasAttempts && !hasIo) return null
  return (
    <div className="px-2.5 pb-2 text-xs">
      {node.last_error && <p className="text-status-error">{node.last_error}</p>}
      {hasAttempts && (
        <span className="mt-1 inline-flex rounded-md border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground">
          {`${node.attempts}/${node.max_attempts} attempts`}
        </span>
      )}
      <IoBlock label="Input" value={node.input} />
      <IoBlock label="Output" value={node.output} />
    </div>
  )
}

/** The run-level reason a run failed; silent when there is none. */
export function RunError({ status, lastError }: { status: string; lastError?: string | null }) {
  if (!lastError) return null
  return (
    <div
      role="alert"
      className="rounded-md border border-status-error/30 bg-status-error/10 px-2.5 py-2 text-xs text-status-error"
      data-status={status}
    >
      <span className="block text-[10px] font-semibold uppercase tracking-[0.08em]">
        Why this run failed
      </span>
      {lastError}
    </div>
  )
}
