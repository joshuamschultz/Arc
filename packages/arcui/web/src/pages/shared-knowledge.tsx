import { useMemo, useState } from 'react'
import { Search, Share2 } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { EmptyState, ErrorState, LoadingRows, QueryState } from '@/components/states'
import { AgentIdentity } from '@/components/AgentIdentity'
import { Input } from '@/components/ui/input'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import {
  useRoster,
  useSharedKnowledge,
  useSharedKnowledgeDetail,
  useSharedKnowledgeSearch,
} from '@/lib/queries'
import { cn } from '@/lib/utils'
import type { AgentIdentityShape, SharedKnowledgeDocument } from '@/lib/types'

/** Data-sensitivity pill — neutral for "unclassified", warm for anything
 *  above it (mirrors `ClassificationBadge` in knowledge-chunks.tsx). */
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

/** A DID the roster hasn't joined still renders through the same component —
 *  every field `AgentIdentityShape` promises is populated with "unknown"
 *  rather than left absent, matching the contract's own convention (H-007). */
function fallbackIdentity(ownerDid: string, ownerDisplay: string): AgentIdentityShape {
  return {
    did: ownerDid,
    host: 'unknown',
    platform: 'unknown',
    type: 'unknown',
    short_id: ownerDid ? ownerDid.slice(-8) : 'unknown',
    name: ownerDisplay || null,
  }
}

/** One shared document: title + excerpt + tags, opens the full-content sheet. */
function DocumentCard({
  doc,
  onOpen,
}: {
  doc: SharedKnowledgeDocument
  onOpen: (identifier: string) => void
}) {
  return (
    <button
      type="button"
      onClick={() => onOpen(doc.identifier)}
      className="w-full rounded-lg border border-border bg-card p-3 text-left shadow-xs transition-colors duration-150 hover:border-primary/25"
    >
      <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2">
        <span className="truncate text-sm font-semibold text-foreground">{doc.title}</span>
        <ClassificationBadge classification={doc.classification} />
      </div>
      <p className="line-clamp-2 text-sm text-muted-foreground">{doc.excerpt}</p>
      {doc.tags.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {doc.tags.map((tag) => (
            <span
              key={tag}
              className="rounded-full border border-border bg-muted/40 px-2 py-0.5 text-[10px] text-muted-foreground"
            >
              {tag}
            </span>
          ))}
        </div>
      )}
    </button>
  )
}

/** Documents grouped by the agent that promoted them, each group headed by
 *  the shared `AgentIdentity` renderer (H-007) — resolved by DID against the
 *  roster when possible, falling back to the server's `owner_display`. */
function GroupedDocuments({
  documents,
  onOpen,
}: {
  documents: SharedKnowledgeDocument[]
  onOpen: (identifier: string) => void
}) {
  const roster = useRoster()

  const identityByDid = useMemo(() => {
    const map = new Map<string, { identity: AgentIdentityShape; color?: string }>()
    for (const agent of roster.data?.agents ?? []) {
      const did = agent.identity?.did ?? agent.did
      if (did && agent.identity) map.set(did, { identity: agent.identity, color: agent.color })
    }
    return map
  }, [roster.data])

  const groups = useMemo(() => {
    const byOwner = new Map<string, { display: string; docs: SharedKnowledgeDocument[] }>()
    for (const doc of documents) {
      const existing = byOwner.get(doc.owner_did)
      if (existing) existing.docs.push(doc)
      else byOwner.set(doc.owner_did, { display: doc.owner_display, docs: [doc] })
    }
    return Array.from(byOwner.entries()).sort((a, b) =>
      a[1].display.localeCompare(b[1].display),
    )
  }, [documents])

  return (
    <div className="space-y-6">
      {groups.map(([ownerDid, group]) => {
        const resolved = identityByDid.get(ownerDid)
        const identity = resolved?.identity ?? fallbackIdentity(ownerDid, group.display)
        return (
          <div key={ownerDid} className="space-y-2">
            <AgentIdentity
              identity={identity}
              fallbackName={group.display}
              color={resolved?.color}
              size="sm"
            />
            <div className="space-y-2 pl-1">
              {group.docs.map((doc) => (
                <DocumentCard key={doc.identifier} doc={doc} onOpen={onOpen} />
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}

/** Flat search hits — the search endpoint doesn't carry owner info, so
 *  results render without a group header. */
function SearchResults({ q, onOpen }: { q: string; onOpen: (identifier: string) => void }) {
  const query = useSharedKnowledgeSearch(q)
  if (query.isLoading) return <LoadingRows rows={5} />
  if (query.isError) return <ErrorState error={query.error} />
  if (!query.data) return null
  if (query.data.hits.length === 0) {
    return <EmptyState title="No matches" description={`Nothing ranked for "${q}".`} />
  }
  return (
    <div className="space-y-2">
      {query.data.hits.map((hit) => (
        <button
          key={hit.identifier}
          type="button"
          onClick={() => onOpen(hit.identifier)}
          className="w-full rounded-lg border border-border bg-card p-3 text-left shadow-xs transition-colors duration-150 hover:border-primary/25"
        >
          <div className="text-sm font-semibold text-foreground">{hit.title}</div>
          <p className="line-clamp-2 text-sm text-muted-foreground">{hit.excerpt}</p>
        </button>
      ))}
    </div>
  )
}

/** Full-content sheet for one shared document — 403 (above clearance) and
 *  404 (missing) surface through the shared `ErrorState`, not a crash. */
function DocumentSheet({
  identifier,
  onOpenChange,
}: {
  identifier: string | null
  onOpenChange: (open: boolean) => void
}) {
  const query = useSharedKnowledgeDetail(identifier)
  return (
    <Sheet open={identifier != null} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-xl">
        {identifier && (
          <QueryState query={query}>
            {(data) => (
              <>
                <SheetHeader className="border-b border-border px-5 py-4">
                  <SheetTitle className="text-sm">{data.title}</SheetTitle>
                  <SheetDescription className="flex flex-wrap items-center gap-2">
                    <ClassificationBadge classification={data.classification} />
                    {data.tags.map((tag) => (
                      <span
                        key={tag}
                        className="rounded-full border border-border bg-muted/40 px-2 py-0.5 text-[10px] text-muted-foreground"
                      >
                        {tag}
                      </span>
                    ))}
                  </SheetDescription>
                </SheetHeader>
                <div className="flex-1 overflow-auto px-5 py-4">
                  <p className="whitespace-pre-wrap text-sm text-foreground">{data.content}</p>
                </div>
              </>
            )}
          </QueryState>
        )}
      </SheetContent>
    </Sheet>
  )
}

/**
 * Fleet-scoped, read-only view of "shared knowledge" — documents agents have
 * promoted into the signed fleet collection (H-027). The per-agent Knowledge
 * page (`/knowledge`) shows what one agent has learned; this page shows what
 * the fleet as a whole has chosen to share, grouped by the agent that shared
 * it. No mutation surface — promotion happens agent-side.
 */
export function SharedKnowledgePage() {
  const [q, setQ] = useState('')
  const [openIdentifier, setOpenIdentifier] = useState<string | null>(null)
  const query = useSharedKnowledge()
  const searching = q.trim().length > 0

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Shared knowledge"
        description="What agents have promoted into the fleet's signed knowledge collection, grouped by owner."
      />
      <div className="flex-1 overflow-auto p-6">
        <div className="mb-4 max-w-sm">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search shared knowledge…"
              className="pl-8"
            />
          </div>
        </div>

        {searching ? (
          <SearchResults q={q} onOpen={setOpenIdentifier} />
        ) : (
          <QueryState
            query={query}
            isEmpty={(d) => d.documents.length === 0}
            empty={
              <EmptyState
                icon={<Share2 className="size-7" />}
                title="Nothing shared yet"
                description="No knowledge has been shared to the fleet yet."
              />
            }
          >
            {(data) => <GroupedDocuments documents={data.documents} onOpen={setOpenIdentifier} />}
          </QueryState>
        )}
      </div>

      <DocumentSheet
        identifier={openIdentifier}
        onOpenChange={(open) => !open && setOpenIdentifier(null)}
      />
    </div>
  )
}
