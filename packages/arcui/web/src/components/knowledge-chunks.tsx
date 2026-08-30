import { useState } from 'react'
import { AlertTriangle, Search } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { EmptyState, ErrorState, LoadingRows, QueryState } from '@/components/states'
import { useChunks, useChunkSearch } from '@/lib/queries'
import { fmtTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { ChunkRecord, ChunkSearchMode } from '@/lib/types'

/** Classification pill — neutral by default, warm for anything above
 *  "unclassified" so a reviewer's eye catches it in a scan. */
function ClassificationBadge({ classification }: { classification: string }) {
  const sensitive = classification.toLowerCase() !== 'unclassified' && classification !== ''
  return (
    <span
      className={cn(
        'rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
        sensitive
          ? 'border-status-warning/40 bg-status-warning/15 text-status-warning'
          : 'border-border bg-muted/40 text-muted-foreground',
      )}
    >
      {classification || 'unlabeled'}
    </span>
  )
}

/** One chunk result/browse card: source + scope + classification + score,
 *  then the (possibly truncated) text preview. */
function ChunkCard({ chunk }: { chunk: ChunkRecord }) {
  return (
    <div className="rounded-lg border border-border bg-card p-3 shadow-xs transition-colors duration-150 hover:border-primary/25">
      <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
        <span className="max-w-[28ch] truncate rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px]">
          {chunk.source}
        </span>
        <div className="flex shrink-0 items-center gap-1.5">
          <ClassificationBadge classification={chunk.classification} />
          <span className="font-mono text-[11px] tabular-nums">
            score {chunk.score.toFixed(3)}
          </span>
        </div>
      </div>
      <p className="whitespace-pre-wrap text-sm text-foreground">
        {chunk.text}
        {chunk.truncated && <span className="text-muted-foreground"> …</span>}
      </p>
      <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[10px] text-muted-foreground">
        <span className="rounded-full border border-border bg-muted/40 px-2 py-0.5 font-mono">
          {chunk.chunk_id}
        </span>
        {chunk.mtime != null && <span>{fmtTime(chunk.mtime)}</span>}
        {chunk.truncated && (
          <span className="rounded-full border border-border bg-muted/40 px-2 py-0.5">
            truncated
          </span>
        )}
      </div>
    </div>
  )
}

/** The degrade banner: loud, not a silent empty result (H-023). */
function DegradedBanner() {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-status-warning/40 bg-status-warning/10 px-3 py-2 text-xs text-status-warning">
      <AlertTriangle className="size-3.5 shrink-0" />
      <span>
        Semantic search is off for this agent — no embedder is wired, so this ran as a literal
        (keyword) search instead.
      </span>
    </div>
  )
}

function ModeToggle({
  mode,
  onChange,
}: {
  mode: ChunkSearchMode
  onChange: (m: ChunkSearchMode) => void
}) {
  return (
    <div className="inline-flex overflow-hidden rounded-md border border-border">
      {(['literal', 'vector'] as const).map((m) => (
        <button
          key={m}
          type="button"
          onClick={() => onChange(m)}
          className={cn(
            'px-2.5 py-1.5 text-xs font-medium capitalize transition-colors duration-150',
            mode === m
              ? 'bg-primary text-primary-foreground'
              : 'bg-transparent text-muted-foreground hover:bg-muted/40',
          )}
        >
          {m}
        </button>
      ))}
    </div>
  )
}

function SearchResults({
  agentId,
  q,
  mode,
}: {
  agentId: string
  q: string
  mode: ChunkSearchMode
}) {
  const query = useChunkSearch(agentId, q, mode)
  if (query.isLoading) return <LoadingRows rows={5} />
  if (query.isError) return <ErrorState error={query.error} />
  if (!query.data) return null
  return (
    <div className="space-y-3">
      {query.data.degraded && <DegradedBanner />}
      {query.data.items.length === 0 ? (
        <EmptyState title="No matches" description={`Nothing ranked for "${q}".`} />
      ) : (
        <div className="space-y-2">
          {query.data.items.map((chunk) => (
            <ChunkCard key={chunk.chunk_id} chunk={chunk} />
          ))}
        </div>
      )}
    </div>
  )
}

/** Browse/search an agent's indexed chunks — embedded (vector) and literal
 *  (BM25) — with source/scope/classification/score metadata (H-023). */
export function ChunkBrowser({ agentId }: { agentId: string }) {
  const [q, setQ] = useState('')
  const [mode, setMode] = useState<ChunkSearchMode>('literal')
  const [offset, setOffset] = useState(0)
  const limit = 20
  const page = useChunks(agentId, limit, offset)

  const searching = q.trim().length > 0

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-full max-w-sm">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search chunks (literal or vector)…"
            className="pl-8"
          />
        </div>
        <ModeToggle mode={mode} onChange={setMode} />
      </div>

      {searching ? (
        <SearchResults agentId={agentId} q={q} mode={mode} />
      ) : (
        <QueryState
          query={page}
          isEmpty={(d) => d.items.length === 0}
          empty={
            <EmptyState
              title="No chunks indexed yet"
              description="This agent hasn't indexed any memory or document chunks."
            />
          }
        >
          {(data) => (
            <div className="space-y-3">
              <div className="space-y-2">
                {data.items.map((chunk) => (
                  <ChunkCard key={chunk.chunk_id} chunk={chunk} />
                ))}
              </div>
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span className="tabular-nums">
                  {data.offset + 1}–{Math.min(data.offset + data.items.length, data.total)} of{' '}
                  {data.total}
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
    </div>
  )
}
