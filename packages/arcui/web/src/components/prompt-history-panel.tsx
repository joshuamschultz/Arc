import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { History } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { CodeBlock } from '@/components/code-block'
import { ErrorState, LoadingRows } from '@/components/states'
import { useAgentPromptHistory, useAgentPromptHistoryDiff } from '@/lib/queries'
import { apiPost, ApiError } from '@/lib/api'
import type { PromptRevertResponse } from '@/lib/types'

type PromptRef = { package: string; name: string }

/** History tab of the prompt drawer (J2 F3). Every save of an override is kept as
 *  a signed, immutable version; this lists them, shows a server-computed diff
 *  between any two (a version, `stock` or `current`), and — in operator mode —
 *  reverts to one. A revert never rewrites history: the server re-signs the old
 *  text as a NEW version, so the revert itself can be reverted. */
export function PromptHistoryPanel({
  agentId,
  prompt,
  operatorMode,
  onChanged,
}: {
  agentId: string
  prompt: PromptRef
  operatorMode: boolean
  onChanged: () => Promise<unknown>
}) {
  const history = useAgentPromptHistory(agentId, prompt, true)
  const queryClient = useQueryClient()
  const [from, setFrom] = useState<string | null>(null)
  const [to, setTo] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const versions = history.data?.versions ?? []
  // Default: what changed in the newest save (previous version -> newest).
  const toRef = to ?? (versions[0] ? String(versions[0].version) : '')
  const fromRef = from ?? (versions[1] ? String(versions[1].version) : versions[0] ? 'stock' : '')
  const diff = useAgentPromptHistoryDiff(agentId, prompt, fromRef, toRef)

  const revert = async (version: number) => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const res = await apiPost<PromptRevertResponse>(
        `/api/agents/${agentId}/prompts/${encodeURIComponent(prompt.package)}/${encodeURIComponent(prompt.name)}/history/${version}/revert`,
      )
      setNotice(res.message)
      setFrom(null)
      setTo(null)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['agent', agentId, 'prompt-history'] }),
        onChanged(),
      ])
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Revert failed')
    } finally {
      setBusy(false)
    }
  }

  if (history.isLoading) return <LoadingRows rows={4} />
  if (history.isError) return <ErrorState error={history.error} />
  if (versions.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        No saved versions yet. Each save of an override is kept here, signed, so you can compare
        and go back.
      </p>
    )
  }

  const options = [
    ...versions.map((v) => ({ value: String(v.version), label: `v${v.version}` })),
    { value: 'stock', label: 'stock' },
  ]

  return (
    <div className="flex flex-col gap-4">
      {error && <p className="text-xs text-destructive">{error}</p>}
      {notice && <p className="text-xs text-status-online">{notice}</p>}

      <ul className="divide-y divide-border rounded-md border border-border text-xs">
        {versions.map((v) => (
          <li key={v.version} className="flex items-center justify-between gap-3 px-3 py-2">
            <div className="flex min-w-0 flex-col gap-0.5">
              <span className="font-mono text-[11px]">
                v{v.version}
                {v.current && <span className="ml-2 text-primary">current</span>}
              </span>
              <span className="truncate text-muted-foreground">
                {v.signed_at ?? 'unknown time'} · {v.signer_did}
              </span>
            </div>
            {operatorMode && !v.current && (
              <Button
                variant="ghost"
                size="sm"
                disabled={busy}
                aria-label={`Revert to v${v.version}`}
                onClick={() => revert(v.version)}
              >
                <History className="size-3.5" /> Revert
              </Button>
            )}
          </li>
        ))}
      </ul>

      <div className="flex flex-wrap items-center gap-2 text-xs">
        <label className="flex items-center gap-1">
          Compare
          <select
            aria-label="Diff from"
            value={fromRef}
            onChange={(e) => setFrom(e.target.value)}
            className="rounded border border-border bg-background px-1 py-0.5 font-mono text-[11px]"
          >
            {options.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1">
          to
          <select
            aria-label="Diff to"
            value={toRef}
            onChange={(e) => setTo(e.target.value)}
            className="rounded border border-border bg-background px-1 py-0.5 font-mono text-[11px]"
          >
            {options.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {diff.isLoading ? (
        <LoadingRows rows={4} />
      ) : diff.isError ? (
        <ErrorState error={diff.error} />
      ) : diff.data?.diff ? (
        <CodeBlock code={diff.data.diff} language="diff" wrap />
      ) : (
        <p className="text-xs text-muted-foreground">These two versions are identical.</p>
      )}
    </div>
  )
}
