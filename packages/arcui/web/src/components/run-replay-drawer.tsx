import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, Wrench } from 'lucide-react'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { JsonBlock } from '@/components/json-block'
import { LoadingRows, ErrorState, EmptyState } from '@/components/states'
import { apiGet } from '@/lib/api'
import { fmtTime, shortId } from '@/lib/format'
import type { Dict, SessionReplayResponse } from '@/lib/types'

/** One step in an agentic run: a turn, tool call, or message. */
function RunStep({ step, index }: { step: Dict; index: number }) {
  const role = String(step.role ?? step.type ?? step.event_type ?? 'step')
  const ts = step.timestamp ?? step.ts
  const tool = step.tool ?? step.name ?? (step.tool_call as Dict | undefined)?.name
  const content =
    typeof step.content === 'string'
      ? step.content
      : typeof step.text === 'string'
        ? step.text
        : JSON.stringify(step, null, 2)

  return (
    <div className="relative pl-6">
      <span className="absolute left-0 top-1.5 size-2 rounded-full bg-primary" />
      <span className="absolute left-[3.5px] top-4 h-full w-px bg-border" />
      <div className="flex items-center gap-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-primary">
          {index + 1}. {role}
        </span>
        {tool != null && (
          <span className="inline-flex items-center gap-1 rounded border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] text-muted-foreground">
            <Wrench className="size-3" />
            {String(tool)}
          </span>
        )}
        {ts != null && (
          <span className="text-[11px] text-muted-foreground">{fmtTime(ts as string)}</span>
        )}
      </div>
      <pre className="mt-1 mb-3 whitespace-pre-wrap break-words rounded-lg border border-border bg-muted/20 p-2.5 font-mono text-xs text-foreground">
        {content}
      </pre>
    </div>
  )
}

/** Replays an agentic run (session): the loop trace, step by step. */
export function RunReplayDrawer({
  agentId,
  sid,
  open,
  onOpenChange,
}: {
  agentId: string | null
  sid: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [page, setPage] = useState(1)
  const q = useQuery<SessionReplayResponse>({
    queryKey: ['agent', agentId, 'session', sid, page],
    queryFn: ({ signal }) =>
      apiGet(`/api/agents/${agentId}/sessions/${sid}?page=${page}`, signal),
    enabled: open && !!agentId && !!sid,
  })

  const data = q.data
  const totalPages = data ? Math.max(1, Math.ceil(data.total / Math.max(1, data.page_size))) : 1

  return (
    <Sheet open={open} onOpenChange={(o) => { if (!o) setPage(1); onOpenChange(o) }}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-2xl">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="font-mono text-sm">Run {shortId(sid ?? '', 16)}</SheetTitle>
          <SheetDescription>
            {agentId}
            {data ? ` · ${data.total} steps` : ''}
          </SheetDescription>
        </SheetHeader>

        <div className="flex-1 overflow-auto p-5">
          {q.isLoading && <LoadingRows rows={6} />}
          {q.isError && <ErrorState error={q.error} />}
          {data && data.messages.length === 0 && <EmptyState title="This run has no recorded steps" />}
          {data && data.messages.length > 0 && (
            <div>
              {data.messages.map((m, i) => (
                <RunStep key={i} step={m} index={(page - 1) * data.page_size + i} />
              ))}
            </div>
          )}
        </div>

        {data && totalPages > 1 && (
          <div className="flex items-center justify-between border-t border-border px-5 py-3 text-xs text-muted-foreground">
            <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
              <ChevronLeft className="size-4" /> Prev
            </Button>
            <span>Page {page} of {totalPages}</span>
            <Button variant="outline" size="sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
              Next <ChevronRight className="size-4" />
            </Button>
          </div>
        )}

        {data && (
          <details className="border-t border-border px-5 py-3">
            <summary className="cursor-pointer text-xs font-medium text-muted-foreground">Raw page JSON</summary>
            <JsonBlock value={data} className="mt-2 max-h-64" />
          </details>
        )}
      </SheetContent>
    </Sheet>
  )
}
