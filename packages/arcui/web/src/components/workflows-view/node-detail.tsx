import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api'
import { useRetryWorkflowNode } from '@/lib/queries'
import type { WorkflowRunNodeStatus } from '@/lib/types'
import { RepeatUnsafeChip } from './repeat-warning'

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

/** Re-run one failed node; the server answers with the refreshed run view. */
function RetryNodeButton({
  runId,
  nodeId,
  repeatUnsafe,
}: {
  runId: string
  nodeId: string
  repeatUnsafe: boolean
}) {
  const retry = useRetryWorkflowNode(runId)
  const [error, setError] = useState<string | null>(null)
  // A tool that cannot dedupe its effect is re-run only on the operator's say-so.
  const [accepted, setAccepted] = useState(false)
  const submit = async () => {
    setError(null)
    try {
      await retry.mutateAsync(nodeId)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not retry node')
    }
  }
  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-2">
      {repeatUnsafe && (
        <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <input
            type="checkbox"
            checked={accepted}
            onChange={(e) => setAccepted(e.target.checked)}
          />
          I accept a repeat of the side effect
        </label>
      )}
      <Button
        size="sm"
        variant="outline"
        disabled={retry.isPending || (repeatUnsafe && !accepted)}
        onClick={submit}
      >
        {retry.isPending ? 'Retrying…' : 'Retry node'}
      </Button>
      {error && <span className="text-[11px] text-status-error">{error}</span>}
    </div>
  )
}

interface NodeDetailProps {
  node: WorkflowRunNodeStatus
  /** Needed (with `runStatus` and `canRetry`) to offer "Retry node". */
  runId?: string
  runStatus?: string
  canRetry?: boolean
}

/** Per-node route, skip reason, failure reason, attempts and bounded input/output. */
export function NodeDetail({ node, runId, runStatus, canRetry }: NodeDetailProps) {
  const hasAttempts = node.attempts != null && node.max_attempts != null
  const hasIo = node.input != null || node.output != null
  const showRoute = node.status === 'routed' && !!node.route
  const showReason = !!node.reason && (node.status === 'skipped' || node.status === 'cancelled')
  const showRetry =
    !!canRetry && !!runId && runStatus === 'failed' && node.status === 'failed'
  const repeatUnsafe = node.idempotent === false && node.status === 'failed'
  if (
    !node.last_error &&
    !hasAttempts &&
    !hasIo &&
    !showRoute &&
    !showReason &&
    !showRetry &&
    !repeatUnsafe
  ) {
    return null
  }
  return (
    <div className="px-2.5 pb-2 text-xs">
      {showRoute && (
        <span className="inline-flex rounded-md border border-status-info/30 bg-status-info/10 px-1.5 py-0.5 text-[10px] text-status-info">
          {`chose route: ${node.route}`}
        </span>
      )}
      {showReason && <p className="text-muted-foreground">{node.reason}</p>}
      {node.last_error && <p className="text-status-error">{node.last_error}</p>}
      {repeatUnsafe && (
        <p className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
          <RepeatUnsafeChip />
          Retrying runs this tool&apos;s side effect again, such as a second send or upload.
        </p>
      )}
      {hasAttempts && (
        <span className="mt-1 inline-flex rounded-md border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground">
          {`${node.attempts}/${node.max_attempts} attempts`}
        </span>
      )}
      <IoBlock label="Input" value={node.input} />
      <IoBlock label="Output" value={node.output} />
      {showRetry && (
        <RetryNodeButton runId={runId} nodeId={node.node_id} repeatUnsafe={repeatUnsafe} />
      )}
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
