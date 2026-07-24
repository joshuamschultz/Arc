import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Pencil, Plus, RotateCcw, X } from 'lucide-react'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { ErrorState, LoadingRows } from '@/components/states'
import { useRubric, useSaveRubric } from '@/lib/queries'
import { apiDelete, ApiError } from '@/lib/api'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import type { RubricDimension } from '@/lib/types'

type Draft = Record<string, RubricDimension>

/** Structured editor for the arcskill judge rubric (COMP-010). Per dimension:
 *  add/remove/edit checklist rows + a calibration (`anti_inflation`) field.
 *  Operator mode adds Edit/Save (PUT structured JSON) and Reset (overlay DELETE);
 *  every mutation is server-gated on the operator role regardless of this toggle. */
export function RubricEditor({
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
  const rubric = useRubric(agentId, prompt)
  const save = useSaveRubric(agentId)
  const queryClient = useQueryClient()
  const [operatorMode] = useOperatorMode()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // A newly selected prompt resets the drawer (state adjusted during render,
  // keyed on the selected prompt name — mirrors the prose drawer).
  const key = prompt ? `${prompt.package}/${prompt.name}` : null
  const [prevKey, setPrevKey] = useState(key)
  if (prevKey !== key) {
    setPrevKey(key)
    setEditing(false)
    setDraft(null)
    setError(null)
    setNotice(null)
  }

  if (prompt == null) return null
  const data = rubric.data

  const invalidate = () =>
    Promise.all([
      queryClient.invalidateQueries({
        queryKey: ['agent', agentId, 'rubric', prompt.package, prompt.name],
      }),
      queryClient.invalidateQueries({ queryKey: ['agent', agentId, 'prompts'] }),
    ])

  const startEdit = () => {
    if (!data) return
    setDraft(structuredClone(data.dimensions))
    setError(null)
    setNotice(null)
    setEditing(true)
  }

  const patchDim = (dim: string, next: RubricDimension) =>
    setDraft((d) => (d ? { ...d, [dim]: next } : d))

  const setChecklistRow = (dim: string, i: number, value: string) => {
    const cur = draft?.[dim]
    if (!cur) return
    const checklist = cur.checklist.map((row, idx) => (idx === i ? value : row))
    patchDim(dim, { ...cur, checklist })
  }
  const addChecklistRow = (dim: string) => {
    const cur = draft?.[dim]
    if (!cur) return
    patchDim(dim, { ...cur, checklist: [...cur.checklist, ''] })
  }
  const removeChecklistRow = (dim: string, i: number) => {
    const cur = draft?.[dim]
    if (!cur) return
    patchDim(dim, { ...cur, checklist: cur.checklist.filter((_, idx) => idx !== i) })
  }
  const setAntiInflation = (dim: string, value: string) => {
    const cur = draft?.[dim]
    if (!cur) return
    patchDim(dim, { ...cur, anti_inflation: value })
  }

  const submit = async () => {
    if (!draft) return
    setBusy(true)
    setError(null)
    try {
      const res = await save.mutateAsync({ prompt, update: { dimensions: draft } })
      setNotice(res.message)
      setEditing(false)
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
      setNotice('Override removed. The rubric resolves to stock on the next run.')
      await invalidate()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Reset failed')
    } finally {
      setBusy(false)
    }
  }

  const dimensions = editing ? draft : (data?.dimensions ?? null)

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-xl">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="font-mono text-sm">
            {prompt.package}/{prompt.name}
          </SheetTitle>
          {data && (
            <SheetDescription className="flex flex-wrap items-center gap-2">
              <Badge variant={data.status === 'overridden' ? 'default' : 'secondary'}>
                {data.status}
              </Badge>
              <span className="text-xs text-muted-foreground">Judge scoring rubric</span>
            </SheetDescription>
          )}
        </SheetHeader>

        <div className="flex items-center justify-end gap-2 border-b border-border px-5 py-2 text-xs">
          {operatorMode && !editing && (
            <Button variant="ghost" size="sm" disabled={!data} onClick={startEdit}>
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
              <Button size="sm" disabled={busy} onClick={submit}>
                {busy ? 'Saving…' : 'Save'}
              </Button>
            </>
          )}
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

        <div className="flex-1 space-y-6 overflow-auto p-5">
          {rubric.isLoading ? (
            <LoadingRows rows={8} />
          ) : rubric.isError ? (
            <ErrorState error={rubric.error} />
          ) : dimensions == null ? (
            <p className="text-xs text-muted-foreground">No rubric to display.</p>
          ) : (
            Object.entries(dimensions).map(([dim, d]) => (
              <section key={dim} className="space-y-2">
                <h3 className="font-mono text-xs font-semibold uppercase tracking-wide text-foreground">
                  {dim}
                </h3>
                <ul className="space-y-1.5">
                  {d.checklist.map((row, i) => (
                    <li key={i} className="flex items-center gap-2">
                      {editing ? (
                        <>
                          <Input
                            value={row}
                            onChange={(e) => setChecklistRow(dim, i, e.target.value)}
                            className="h-8 font-mono text-xs"
                            aria-label={`${dim} checklist row ${i + 1}`}
                          />
                          <Button
                            variant="ghost"
                            size="icon"
                            className="size-8 shrink-0"
                            disabled={busy}
                            onClick={() => removeChecklistRow(dim, i)}
                            aria-label={`Remove ${dim} checklist row ${i + 1}`}
                          >
                            <X className="size-3.5" />
                          </Button>
                        </>
                      ) : (
                        <span className="text-xs text-foreground before:mr-2 before:text-muted-foreground before:content-['•']">
                          {row}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
                {editing && (
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={busy}
                    onClick={() => addChecklistRow(dim)}
                  >
                    <Plus className="size-3.5" /> Add row
                  </Button>
                )}
                <div className="space-y-1 pt-1">
                  <label className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                    Calibration
                  </label>
                  {editing ? (
                    <Textarea
                      value={d.anti_inflation}
                      onChange={(e) => setAntiInflation(dim, e.target.value)}
                      spellCheck={false}
                      className="min-h-[60px] font-mono text-xs"
                      aria-label={`${dim} calibration`}
                    />
                  ) : (
                    <p className="text-xs text-muted-foreground">{d.anti_inflation}</p>
                  )}
                </div>
              </section>
            ))
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
