import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { JsonBlock } from '@/components/json-block'
import { LlmContent } from '@/components/llm-content-renderer'
import { LoadingRows, ErrorState, EmptyState } from '@/components/states'
import { apiGet } from '@/lib/api'
import { fmtTime, shortId } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { Dict, SessionReplayResponse } from '@/lib/types'

// U7 — the session view read like N copies of the accumulated context: the old
// renderer dumped every JSONL record (including non-message checkpoint/summary
// rows) as raw JSON, and repeated any context a turn re-showed. This renders the
// session as the actual back-and-forth: message records only, each unique turn
// once, with structured (not JSON-blob) content.

/** A JSONL record is a conversation turn when it is a role-bearing message —
 *  not a checkpoint / summary / index bookkeeping row. */
function isMessage(rec: Dict): boolean {
  const type = rec.type
  if (type === 'checkpoint' || type === 'summary') return false
  return typeof rec.role === 'string' && rec.content !== undefined && rec.content !== null
}

/** Stable signature for a turn so re-shown context collapses to one bubble. */
function signature(rec: Dict): string {
  const content =
    typeof rec.content === 'string' ? rec.content : JSON.stringify(rec.content ?? null)
  return `${String(rec.role)}::${content}`
}

/** Keep message turns only, in order, dropping any turn whose (role, content)
 *  already appeared — this folds the accumulated per-turn context into a single
 *  clean transcript. */
function toTranscript(records: Dict[]): Dict[] {
  const seen = new Set<string>()
  const out: Dict[] = []
  for (const rec of records) {
    if (!isMessage(rec)) continue
    const sig = signature(rec)
    if (seen.has(sig)) continue
    seen.add(sig)
    out.push(rec)
  }
  return out
}

const ROLE_STYLE: Record<string, string> = {
  user: 'border-primary/30 bg-primary/5',
  assistant: 'border-border bg-muted/20',
  system: 'border-border bg-muted/40',
  tool: 'border-border bg-muted/10',
}

/** One conversation turn as a chat bubble with structured content. */
function ChatTurn({ turn }: { turn: Dict }) {
  const role = String(turn.role ?? 'message')
  const ts = turn.timestamp ?? turn.ts
  return (
    <div className={cn('rounded-lg border p-3', ROLE_STYLE[role] ?? 'border-border bg-muted/20')}>
      <div className="mb-2 flex items-center gap-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-primary">
          {role}
        </span>
        {ts != null && (
          <span className="text-[11px] text-muted-foreground">{fmtTime(ts as string)}</span>
        )}
      </div>
      <LlmContent content={turn.content} />
    </div>
  )
}

/** Replays a session as a clean chat: user/assistant turns once each, in order. */
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
  const transcript = data ? toTranscript(data.messages) : []

  return (
    <Sheet open={open} onOpenChange={(o) => { if (!o) setPage(1); onOpenChange(o) }}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-2xl">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="font-mono text-sm">Session {shortId(sid ?? '', 16)}</SheetTitle>
          <SheetDescription>
            {agentId}
            {data ? ` · ${transcript.length} turns` : ''}
          </SheetDescription>
        </SheetHeader>

        <div className="flex-1 space-y-3 overflow-auto p-5">
          {q.isLoading && <LoadingRows rows={6} />}
          {q.isError && <ErrorState error={q.error} />}
          {data && transcript.length === 0 && <EmptyState title="This session has no conversation turns" />}
          {transcript.map((turn, i) => (
            <ChatTurn key={i} turn={turn} />
          ))}
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
