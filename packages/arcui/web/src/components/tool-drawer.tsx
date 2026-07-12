import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Pencil } from 'lucide-react'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { JsonBlock } from '@/components/json-block'
import { ErrorState, LoadingRows } from '@/components/states'
import { ClassificationBadge } from '@/components/tools-table'
import { useAgentToolDetail } from '@/lib/queries'
import { apiPut, ApiError } from '@/lib/api'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import type { FileWriteResponse } from '@/lib/types'

/** Tool source detail drawer (U6). View mode renders the `.py` source as a
 *  code block; operator mode adds Edit/Save for agent/workspace-authored
 *  tools, saving through the same `PUT /files/read` route `FileViewer` uses —
 *  builtins and module tools stay read-only (no write target from the
 *  backend). */
export function ToolDrawer({
  agentId,
  toolName,
  open,
  onOpenChange,
}: {
  agentId: string
  toolName: string | null
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const detail = useAgentToolDetail(agentId, toolName)
  const queryClient = useQueryClient()
  const [operatorMode] = useOperatorMode()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saveResult, setSaveResult] = useState<FileWriteResponse | null>(null)

  // A newly selected tool always opens in view mode with a clean save state
  // (state adjusted during render, keyed on the selected tool).
  const [prevTool, setPrevTool] = useState(toolName)
  if (prevTool !== toolName) {
    setPrevTool(toolName)
    setEditing(false)
    setSaveError(null)
    setSaveResult(null)
  }

  if (toolName == null) return null

  const startEdit = () => {
    setDraft(detail.data?.content ?? '')
    setSaveError(null)
    setSaveResult(null)
    setEditing(true)
  }

  const save = async () => {
    const { write_root, write_path } = detail.data ?? {}
    if (!write_root || !write_path) return
    setSaving(true)
    setSaveError(null)
    try {
      const res = await apiPut<FileWriteResponse>(
        `/api/agents/${agentId}/files/read?root=${write_root}&path=${encodeURIComponent(write_path)}`,
        { content: draft },
      )
      setSaveResult(res)
      setEditing(false)
      await queryClient.invalidateQueries({ queryKey: ['agent', agentId, 'tool', toolName] })
    } catch (e) {
      setSaveError(e instanceof ApiError ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-xl">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="font-mono text-sm">{toolName}</SheetTitle>
          {detail.data && (
            <SheetDescription className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs">{detail.data.transport || '—'}</span>
              <ClassificationBadge value={detail.data.classification} />
              {detail.data.description && <span>{detail.data.description}</span>}
            </SheetDescription>
          )}
        </SheetHeader>

        <div className="flex items-center justify-between gap-3 border-b border-border px-5 py-2 text-xs text-muted-foreground">
          <span className="truncate rounded border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px]">
            {detail.data?.source_path}
          </span>
          <div className="flex shrink-0 items-center gap-2">
            {operatorMode && detail.data?.editable && !editing && (
              <Button variant="ghost" size="sm" onClick={startEdit}>
                <Pencil className="size-3.5" /> Edit
              </Button>
            )}
            {editing && (
              <>
                <Button variant="ghost" size="sm" disabled={saving} onClick={() => setEditing(false)}>
                  Cancel
                </Button>
                <Button size="sm" disabled={saving} onClick={save}>
                  {saving ? 'Saving…' : 'Save'}
                </Button>
              </>
            )}
          </div>
        </div>

        {saveError && (
          <div className="border-b border-destructive/30 bg-destructive/10 px-5 py-2 text-xs text-destructive">
            {saveError}
          </div>
        )}
        {saveResult?.signature_stale && (
          <div className="border-b border-status-warning/30 bg-status-warning/10 px-5 py-2 text-xs text-status-warning">
            {saveResult.message}
          </div>
        )}
        {saveResult && !saveResult.signature_stale && (
          <div className="border-b border-status-online/30 bg-status-online/10 px-5 py-2 text-xs text-status-online">
            {saveResult.message}
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
          ) : detail.data?.content ? (
            <JsonBlock value={detail.data.content} className="whitespace-pre" />
          ) : (
            <p className="text-xs text-muted-foreground">No source available for this tool.</p>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
