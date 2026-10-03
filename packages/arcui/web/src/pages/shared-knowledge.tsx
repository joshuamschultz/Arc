import { useMemo, useState } from 'react'
import { Share2 } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { EmptyState, ErrorState, LoadingRows, QueryState } from '@/components/states'
import { AgentIdentity } from '@/components/AgentIdentity'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { SearchInput } from '@/components/ui/search-input'
import {
  ClassificationBadge,
  SharedDocumentSheet,
} from '@/components/knowledge-shared-drawer'
import { useRoster, useSharedKnowledgeSearch } from '@/lib/queries'
import { useSharedList, type SharedDocument, type SharedKind } from '@/lib/queries-shared'
import { relativeTime } from '@/lib/format'
import type { AgentIdentityShape } from '@/lib/types'

type Tab = 'all' | SharedKind
const TABS: ReadonlyArray<{ value: Tab; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'insight', label: 'Insights' },
  { value: 'procedure', label: 'Procedures' },
  { value: 'entity', label: 'Entities' },
]

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

/** One shared document: real title, body only when it adds something beyond
 *  the title, owner-side contributors and promoted time. Opens the drawer. */
function DocumentCard({
  doc,
  onOpen,
}: {
  doc: SharedDocument
  onOpen: (identifier: string) => void
}) {
  const showExcerpt = doc.excerpt.trim() !== '' && doc.excerpt.trim() !== doc.title.trim()
  return (
    <button
      type="button"
      onClick={() => onOpen(doc.identifier)}
      className="w-full rounded-lg border border-border bg-card p-3 text-left shadow-xs transition-colors duration-150 hover:border-primary/25"
    >
      <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2">
        <span className="truncate text-sm font-semibold text-foreground">{doc.title}</span>
        <span className="flex items-center gap-1.5">
          {doc.demotion && (
            <span className="rounded-full border border-destructive/40 bg-destructive/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-destructive">
              Removed
            </span>
          )}
          <ClassificationBadge classification={doc.classification} />
        </span>
      </div>
      {showExcerpt && <p className="line-clamp-2 text-sm text-muted-foreground">{doc.excerpt}</p>}
      {doc.demotion && (
        <p className="mt-1 text-xs text-destructive">Reason: {doc.demotion.reason}</p>
      )}
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
        {doc.contributors.length > 0 && (
          <span>With {doc.contributors.map((c) => c.display).join(', ')}</span>
        )}
        {doc.promoted_at && <span>Promoted {relativeTime(doc.promoted_at)}</span>}
      </div>
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
  documents: SharedDocument[]
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
    const byOwner = new Map<string, { display: string; docs: SharedDocument[] }>()
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

/**
 * Fleet-scoped view of shared knowledge: documents agents (or an operator)
 * promoted into the signed fleet collection, in typed tabs. An operator can
 * permanently remove a card; removed cards are hidden unless asked for.
 */
export function SharedKnowledgePage() {
  const [q, setQ] = useState('')
  const [tab, setTab] = useState<Tab>('all')
  const [includeRemoved, setIncludeRemoved] = useState(false)
  const [openIdentifier, setOpenIdentifier] = useState<string | null>(null)
  const query = useSharedList(tab === 'all' ? null : tab, includeRemoved)
  const searching = q.trim().length > 0
  const counts = query.data?.counts

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Shared knowledge"
        description="What agents have promoted into the fleet's signed knowledge collection, grouped by owner."
      />
      <div className="flex-1 overflow-auto p-6">
        <div className="mb-4 flex flex-wrap items-center gap-4">
          <SearchInput
            aria-label="Search shared knowledge"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search shared knowledge…"
            helpKey="shared_knowledge.search"
            helpRoute="shared-knowledge"
          />
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={includeRemoved}
              onChange={(e) => setIncludeRemoved(e.target.checked)}
            />
            Include removed
          </label>
        </div>

        {searching ? (
          <SearchResults q={q} onOpen={setOpenIdentifier} />
        ) : (
          <div className="space-y-4">
            <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)}>
              <TabsList>
                {TABS.map((t) => (
                  <TabsTrigger key={t.value} value={t.value}>
                    {t.label}
                    {counts && (
                      <span className="ml-1.5 tabular-nums text-muted-foreground">
                        {counts[t.value === 'all' ? 'all' : t.value]}
                      </span>
                    )}
                  </TabsTrigger>
                ))}
              </TabsList>
            </Tabs>
            <QueryState
              query={query}
              isEmpty={(d) => d.documents.length === 0}
              empty={
                <EmptyState
                  icon={<Share2 className="size-7" />}
                  title="Nothing shared yet"
                  description="No knowledge of this kind has been shared to the fleet yet."
                />
              }
            >
              {(data) => <GroupedDocuments documents={data.documents} onOpen={setOpenIdentifier} />}
            </QueryState>
          </div>
        )}
      </div>

      <SharedDocumentSheet identifier={openIdentifier} onClose={() => setOpenIdentifier(null)} />
    </div>
  )
}
