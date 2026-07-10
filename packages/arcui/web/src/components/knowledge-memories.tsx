import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Pencil, Search, Trash2, X } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { EmptyState, ErrorState, LoadingRows, QueryState } from '@/components/states'
import { useAgentMemories, useMemoryLinks, useMemorySearch } from '@/lib/queries'
import { apiDelete, apiPatch, ApiError } from '@/lib/api'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import { fmtPercent, fmtTime, relativeTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { MemoryRecord, MutationResponse } from '@/lib/types'

function ImportanceBadge({ n }: { n: number }) {
  const tone =
    n >= 8
      ? 'bg-status-online/15 text-status-online'
      : n >= 4
        ? 'bg-status-warning/15 text-status-warning'
        : 'bg-muted text-muted-foreground'
  return <span className={cn('rounded px-1.5 py-0.5 font-mono text-[11px]', tone)}>{n}/10</span>
}

function RecencyBar({ recency }: { recency: number }) {
  const pct = Math.max(0, Math.min(100, Math.round(recency * 100)))
  const bg = pct >= 60 ? 'bg-status-online' : pct >= 25 ? 'bg-status-warning' : 'bg-status-error'
  return (
    <div className="flex items-center gap-1.5">
      <div className="h-1.5 w-12 overflow-hidden rounded-full bg-muted">
        <div className={cn('h-full rounded-full', bg)} style={{ width: `${pct}%` }} />
      </div>
      <span className="font-mono text-[11px] text-muted-foreground">{fmtPercent(recency)}</span>
    </div>
  )
}

/** Row-detail drawer: metadata, entity links (clickable into Entities), and,
 *  in operator mode, edit-text / edit-importance / delete affordances. */
function MemoryDetail({
  agentId,
  record,
  open,
  onOpenChange,
  onNavigateEntity,
}: {
  agentId: string
  record: MemoryRecord | null
  open: boolean
  onOpenChange: (o: boolean) => void
  onNavigateEntity: (slug: string) => void
}) {
  const queryClient = useQueryClient()
  const [operatorMode] = useOperatorMode()
  const links = useMemoryLinks(agentId, record?.entry_id ?? null)

  const [editingText, setEditingText] = useState(false)
  const [draftText, setDraftText] = useState('')
  const [editingImportance, setEditingImportance] = useState(false)
  const [draftImportance, setDraftImportance] = useState(1)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (record == null) return null

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ['agent', agentId, 'knowledge', 'memories'] })

  const startEditText = () => {
    setDraftText(record.text)
    setError(null)
    setEditingText(true)
  }
  const startEditImportance = () => {
    setDraftImportance(record.importance)
    setError(null)
    setEditingImportance(true)
  }

  async function submitPatch(body: Record<string, unknown>) {
    // The route only ever returns 200 when every sub-op applied; any failure
    // (edit or a viewer-role 403) is a non-2xx that apiPatch throws for, with
    // the server's real reason surfaced by api.ts's parseError.
    setBusy(true)
    setError(null)
    try {
      await apiPatch<MutationResponse>(
        `/api/agents/${agentId}/knowledge/memories/${record!.entry_id}`,
        body,
      )
      await invalidate()
      setEditingText(false)
      setEditingImportance(false)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Edit failed')
    } finally {
      setBusy(false)
    }
  }

  async function submitDelete() {
    setBusy(true)
    setError(null)
    try {
      await apiDelete<MutationResponse>(`/api/agents/${agentId}/knowledge/memories/${record!.entry_id}`)
      await invalidate()
      onOpenChange(false)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Delete failed')
    } finally {
      setBusy(false)
      setConfirmDelete(false)
    }
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-xl">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="text-sm">Memory {record.entry_id}</SheetTitle>
          <SheetDescription>
            {record.kind} · {record.classification} · {fmtTime(record.created)}
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 space-y-5 overflow-auto p-5">
          {error && (
            <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {error}
            </div>
          )}

          <section className="space-y-2">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Text</h3>
              {operatorMode && !editingText && (
                <Button variant="ghost" size="sm" onClick={startEditText}>
                  <Pencil className="size-3.5" /> Edit
                </Button>
              )}
            </div>
            {editingText ? (
              <div className="space-y-2">
                <textarea
                  value={draftText}
                  onChange={(e) => setDraftText(e.target.value)}
                  className="h-32 w-full resize-none rounded-lg border border-border bg-muted/30 p-2 text-sm text-foreground outline-none focus:ring-1 focus:ring-ring"
                />
                <div className="flex justify-end gap-2">
                  <Button variant="ghost" size="sm" disabled={busy} onClick={() => setEditingText(false)}>
                    Cancel
                  </Button>
                  <Button size="sm" disabled={busy} onClick={() => submitPatch({ text: draftText })}>
                    {busy ? 'Saving…' : 'Save'}
                  </Button>
                </div>
              </div>
            ) : (
              <p className="whitespace-pre-wrap rounded-lg border border-border bg-muted/20 p-3 text-sm text-foreground">
                {record.text}
              </p>
            )}
          </section>

          <section className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <div className="text-[11px] uppercase tracking-wide text-muted-foreground">Created</div>
              <div className="text-foreground">{fmtTime(record.created)}</div>
              <div className="text-xs text-muted-foreground">{relativeTime(record.created)}</div>
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wide text-muted-foreground">Recency</div>
              <RecencyBar recency={record.recency} />
            </div>
            <div>
              <div className="mb-1 flex items-center gap-2 text-[11px] uppercase tracking-wide text-muted-foreground">
                Importance
                {operatorMode && !editingImportance && (
                  <button
                    type="button"
                    onClick={startEditImportance}
                    className="cursor-pointer text-primary hover:underline"
                  >
                    edit
                  </button>
                )}
              </div>
              {editingImportance ? (
                <div className="flex items-center gap-2">
                  <input
                    type="number"
                    min={1}
                    max={10}
                    value={draftImportance}
                    onChange={(e) => setDraftImportance(Number(e.target.value))}
                    className="h-7 w-14 rounded border border-border bg-muted/30 px-1.5 text-sm text-foreground outline-none focus:ring-1 focus:ring-ring"
                  />
                  <Button variant="ghost" size="icon-xs" disabled={busy} onClick={() => setEditingImportance(false)}>
                    <X className="size-3" />
                  </Button>
                  <Button
                    size="icon-xs"
                    disabled={busy}
                    onClick={() => submitPatch({ importance: draftImportance })}
                  >
                    <Pencil className="size-3" />
                  </Button>
                </div>
              ) : (
                <ImportanceBadge n={record.importance} />
              )}
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wide text-muted-foreground">Source</div>
              <div className="truncate font-mono text-xs text-foreground">{record.source}</div>
            </div>
          </section>

          <section className="space-y-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Links</h3>
            <QueryState
              query={links}
              isEmpty={(d) => d.items.length === 0}
              empty={<p className="text-xs text-muted-foreground">No linked entities.</p>}
            >
              {(data) => (
                <div className="flex flex-wrap gap-1.5">
                  {data.items.map((l, i) =>
                    l.target_type === 'entity' ? (
                      <button
                        key={i}
                        type="button"
                        onClick={() => onNavigateEntity(l.target_id)}
                        className="cursor-pointer rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-xs text-primary hover:bg-primary/20"
                      >
                        {l.target_id} · {l.kind}
                      </button>
                    ) : (
                      <span
                        key={i}
                        className="rounded-full border border-border bg-muted/40 px-2 py-0.5 text-xs text-muted-foreground"
                      >
                        {l.target_id} · {l.kind}
                      </span>
                    ),
                  )}
                </div>
              )}
            </QueryState>
          </section>

          {operatorMode && (
            <section className="border-t border-border pt-4">
              {confirmDelete ? (
                <div className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                  <span className="flex-1">Delete this memory permanently?</span>
                  <Button variant="ghost" size="sm" disabled={busy} onClick={() => setConfirmDelete(false)}>
                    Cancel
                  </Button>
                  <Button variant="destructive" size="sm" disabled={busy} onClick={submitDelete}>
                    {busy ? 'Deleting…' : 'Confirm delete'}
                  </Button>
                </div>
              ) : (
                <Button variant="destructive" size="sm" onClick={() => setConfirmDelete(true)}>
                  <Trash2 className="size-3.5" /> Delete memory
                </Button>
              )}
            </section>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}

function SearchResults({ agentId, q }: { agentId: string; q: string }) {
  const query = useMemorySearch(agentId, q)
  if (query.isLoading) return <LoadingRows rows={5} />
  if (query.isError) return <ErrorState error={query.error} />
  if (!query.data || query.data.items.length === 0)
    return <EmptyState title="No matches" description={`Nothing ranked for "${q}".`} />
  return (
    <div className="space-y-2">
      {query.data.items.map((r, i) => (
        <div key={i} className="rounded-lg border border-border bg-card p-3">
          <div className="mb-1 flex items-center justify-between text-xs text-muted-foreground">
            <span className="truncate font-mono">{r.source}</span>
            <span className="shrink-0 font-mono">score {r.score.toFixed(3)}</span>
          </div>
          <p className="text-sm text-foreground">{r.content}</p>
          <div className="mt-1.5 flex gap-1.5 text-[11px] text-muted-foreground">
            <span className="rounded bg-muted/40 px-1.5 py-0.5">{r.kind}</span>
            <span className="rounded bg-muted/40 px-1.5 py-0.5">{r.classification}</span>
            <span className="rounded bg-muted/40 px-1.5 py-0.5">{r.confidence}</span>
          </div>
        </div>
      ))}
    </div>
  )
}

/** Browse/search an agent's episodic memories (COMP-003). Search (`?q=`) shows
 *  production-ranked recall previews; the plain list is the curatable surface
 *  with metadata columns and, for operators, edit/delete. */
export function MemoryBrowser({
  agentId,
  onNavigateEntity,
}: {
  agentId: string
  onNavigateEntity: (slug: string) => void
}) {
  const [q, setQ] = useState('')
  const [offset, setOffset] = useState(0)
  const [selected, setSelected] = useState<MemoryRecord | null>(null)
  const limit = 50
  const page = useAgentMemories(agentId, limit, offset)

  const searching = q.trim().length > 0

  return (
    <div className="space-y-3">
      <div className="relative w-full max-w-sm">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search memories (ranked recall)…"
          className="pl-8"
        />
      </div>

      {searching ? (
        <SearchResults agentId={agentId} q={q} />
      ) : (
        <QueryState
          query={page}
          isEmpty={(d) => d.items.length === 0}
          empty={
            <EmptyState
              title="No memories recorded yet"
              description="This agent hasn't captured any episodic memories."
            />
          }
        >
          {(data) => (
            <div className="space-y-3">
              <div className="overflow-hidden rounded-xl border border-border bg-card">
                <table className="w-full text-sm">
                  <thead className="bg-muted/40">
                    <tr className="border-b border-border">
                      <th className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                        Created
                      </th>
                      <th className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                        Text
                      </th>
                      <th className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                        Recency
                      </th>
                      <th className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                        Importance
                      </th>
                      <th className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                        Source
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map((m) => (
                      <tr
                        key={m.entry_id}
                        onClick={() => setSelected(m)}
                        className="cursor-pointer border-b border-border/60 last:border-0 hover:bg-muted/40"
                      >
                        <td className="px-3 py-2 align-top text-xs text-muted-foreground">
                          {relativeTime(m.created)}
                        </td>
                        <td className="max-w-md truncate px-3 py-2 align-top text-foreground">{m.text}</td>
                        <td className="px-3 py-2 align-top">
                          <RecencyBar recency={m.recency} />
                        </td>
                        <td className="px-3 py-2 align-top">
                          <ImportanceBadge n={m.importance} />
                        </td>
                        <td className="max-w-[16ch] truncate px-3 py-2 align-top font-mono text-xs text-muted-foreground">
                          {m.source}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>
                  {data.offset + 1}–{Math.min(data.offset + data.items.length, data.total)} of {data.total}
                </span>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={offset === 0}
                    onClick={() => setOffset(Math.max(0, offset - limit))}
                  >
                    Previous
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={offset + limit >= data.total}
                    onClick={() => setOffset(offset + limit)}
                  >
                    Next
                  </Button>
                </div>
              </div>
            </div>
          )}
        </QueryState>
      )}

      <MemoryDetail
        agentId={agentId}
        record={selected}
        open={selected != null}
        onOpenChange={(o) => !o && setSelected(null)}
        onNavigateEntity={onNavigateEntity}
      />
    </div>
  )
}
