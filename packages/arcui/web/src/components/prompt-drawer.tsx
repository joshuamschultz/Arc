import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Pencil, RotateCcw } from 'lucide-react'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { CodeBlock } from '@/components/code-block'
import { ErrorState, LoadingRows } from '@/components/states'
import { useAgentPromptDetail } from '@/lib/queries'
import { apiPut, apiDelete, ApiError } from '@/lib/api'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import type { PromptWriteResponse } from '@/lib/types'

type View = 'stock' | 'effective' | 'diff'

/** System-prompt detail drawer (COMP-012). A `stock | effective | diff` toggle
 *  renders the server-computed unified diff (no diff library in the browser);
 *  operator mode adds Edit/Save (PUT) and Reset-to-stock (DELETE). Every mutation
 *  is server-gated on the operator role regardless of this client toggle. */
export function PromptDrawer({
  agentId,
  prompt,
  open,
  onOpenChange,
}: {
  agentId: string
  prompt: { package: string; name: string } | null
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const detail = useAgentPromptDetail(agentId, prompt)
  const queryClient = useQueryClient()
  const [operatorMode] = useOperatorMode()
  const [view, setView] = useState<View>('effective')
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // A newly selected prompt resets to a clean view (state adjusted during render,
  // keyed on the selected prompt name).
  const key = prompt ? `${prompt.package}/${prompt.name}` : null
  const [prevKey, setPrevKey] = useState(key)
  if (prevKey !== key) {
    setPrevKey(key)
    setView('effective')
    setEditing(false)
    setError(null)
    setNotice(null)
  }

  if (prompt == null) return null
  const data = detail.data

  const invalidate = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ['agent', agentId, 'prompt', prompt.package, prompt.name] }),
      queryClient.invalidateQueries({ queryKey: ['agent', agentId, 'prompts'] }),
    ])

  const startEdit = () => {
    setDraft(data?.effective ?? '')
    setError(null)
    setNotice(null)
    setEditing(true)
  }

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      const res = await apiPut<PromptWriteResponse>(
        `/api/agents/${agentId}/prompts/${encodeURIComponent(prompt.package)}/${encodeURIComponent(prompt.name)}`,
        { content: draft },
      )
      setNotice(res.message)
      setEditing(false)
      await invalidate()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Save failed')
    } finally {
      setBusy(false)
    }
  }

  const reset = async () => {
    setBusy(true)
    setError(null)
    try {
      await apiDelete(
        `/api/agents/${agentId}/prompts/${encodeURIComponent(prompt.package)}/${encodeURIComponent(prompt.name)}`,
      )
      setNotice('Override removed. The prompt resolves to stock on the next run.')
      await invalidate()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Reset failed')
    } finally {
      setBusy(false)
    }
  }

  const body = view === 'stock' ? data?.stock : view === 'diff' ? data?.diff : data?.effective

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-2xl lg:max-w-3xl">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="font-mono text-sm">
            {prompt.package}/{prompt.name}
          </SheetTitle>
          {data && (
            <SheetDescription className="flex flex-wrap items-center gap-2">
              <Badge variant={data.status === 'overridden' ? 'default' : 'secondary'}>
                {data.status}
              </Badge>
              <span className="text-xs text-muted-foreground">{data.description}</span>
            </SheetDescription>
          )}
        </SheetHeader>

        <div className="flex items-center justify-between gap-3 border-b border-border px-5 py-2 text-xs">
          <div className="flex items-center gap-1">
            {(['stock', 'effective', 'diff'] as View[]).map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => setView(v)}
                disabled={editing}
                className={
                  'rounded px-2 py-0.5 font-mono text-[11px] transition-colors ' +
                  (view === v
                    ? 'bg-primary/15 text-primary'
                    : 'text-muted-foreground hover:text-foreground disabled:opacity-40')
                }
              >
                {v}
              </button>
            ))}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {operatorMode && !editing && (
              <Button variant="ghost" size="sm" onClick={startEdit}>
                <Pencil className="size-3.5" /> Edit
              </Button>
            )}
            {operatorMode && !editing && data?.status === 'overridden' && (
              <Button variant="ghost" size="sm" disabled={busy} onClick={reset}>
                <RotateCcw className="size-3.5" /> Reset
              </Button>
            )}
            {editing && (
              <>
                <Button variant="ghost" size="sm" disabled={busy} onClick={() => setEditing(false)}>
                  Cancel
                </Button>
                <Button size="sm" disabled={busy} onClick={save}>
                  {busy ? 'Saving…' : 'Save'}
                </Button>
              </>
            )}
          </div>
        </div>

        {error && (
          <div className="border-b border-destructive/30 bg-destructive/10 px-5 py-2 text-xs text-destructive">
            {error}
          </div>
        )}
        {notice && (
          <div className="border-b border-status-online/30 bg-status-online/10 px-5 py-2 text-xs text-status-online">
            {notice}
          </div>
        )}

        <div className="flex-1 overflow-auto p-5">
          {detail.isLoading ? (
            <LoadingRows rows={8} />
          ) : detail.isError ? (
            <ErrorState error={detail.error} />
          ) : editing ? (
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              spellCheck={false}
              className="h-full min-h-[320px] w-full resize-none rounded-md border border-border bg-muted/30 p-3 font-mono text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
            />
          ) : view === 'diff' && !body ? (
            <p className="text-xs text-muted-foreground">
              No override — the effective prompt is identical to stock.
            </p>
          ) : (
            <CodeBlock code={body ?? ''} language={view === 'diff' ? 'diff' : 'markdown'} />
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
