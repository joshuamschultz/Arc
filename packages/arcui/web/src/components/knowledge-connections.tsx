import { useState, type ReactNode } from 'react'
import { Activity, Database, FileText, FolderTree, Plug, Waypoints } from 'lucide-react'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Input } from '@/components/ui/input'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { JsonBlock } from '@/components/json-block'
import { EmptyState, QueryState } from '@/components/states'
import {
  useBlobFolders,
  useDatastoreQuery,
  useDatastoreTables,
  useDocuments,
  useIndexHealth,
  useProvenance,
  useSourceMapping,
  useSources,
} from '@/lib/queries'
import { cn } from '@/lib/utils'
import type { EntityRecord } from '@/lib/types'

// Source entities are stored under the slug `source-<id>`; every connector
// route keys off the raw `<id>`, so this is the one place that bridges the two.
const stripSourcePrefix = (slug: string) => slug.replace(/^source-/, '')

const SECTIONS = [
  { value: 'sources', label: 'Sources' },
  { value: 'documents', label: 'Documents' },
  { value: 'datastore', label: 'Datastore' },
  { value: 'blob', label: 'Blob folders' },
  { value: 'provenance', label: 'Provenance' },
  { value: 'health', label: 'Index health' },
] as const

type Section = (typeof SECTIONS)[number]['value']
type DatastoreOp = 'get_record' | 'find' | 'list'

// --- Shared bits ------------------------------------------------------------

const TH_CLASS =
  'px-3 py-2 text-left text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground'

function Th({ children }: { children: ReactNode }) {
  return <th className={TH_CLASS}>{children}</th>
}

/** A source entity's facts, rendered as plain metadata rows (the connector
 *  facts are free-form, so they are shown verbatim rather than parsed). */
function FactList({ facts }: { facts: string[] }) {
  if (facts.length === 0) {
    return <p className="text-xs text-muted-foreground">No recorded facts.</p>
  }
  return (
    <ul className="space-y-1.5">
      {facts.map((f, i) => (
        <li
          key={i}
          className="rounded-lg border border-border bg-muted/20 px-3 py-2 text-sm text-foreground"
        >
          {f}
        </li>
      ))}
    </ul>
  )
}

function Chip({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-full border border-border bg-muted/40 px-2 py-0.5 text-xs text-muted-foreground">
      {children}
    </span>
  )
}

function MonoChip({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
      {children}
    </span>
  )
}

/** A picker over an agent's connector sources. Value is the raw source id. */
function SourceSelect({
  agentId,
  value,
  onChange,
  placeholder = 'Select source',
}: {
  agentId: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
}) {
  const sources = useSources(agentId)
  const items = sources.data?.items ?? []
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className="w-56">
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {items.map((s) => {
          const id = stripSourcePrefix(s.slug)
          return (
            <SelectItem key={s.slug} value={id}>
              {s.name || id}
            </SelectItem>
          )
        })}
      </SelectContent>
    </Select>
  )
}

/** A read-only table of entity rows (name + type + classification + fact count)
 *  that opens a detail Sheet on click — the shared shell for sources, blob
 *  folders, and datastore tables. */
function EntityTable({
  items,
  onSelect,
  typeLabel = 'Type',
}: {
  items: EntityRecord[]
  onSelect?: (e: EntityRecord) => void
  typeLabel?: string
}) {
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-card shadow-xs">
      <table className="w-full text-sm">
        <thead className="bg-muted/40">
          <tr className="border-b border-border">
            <Th>Name</Th>
            <Th>{typeLabel}</Th>
            <Th>Classification</Th>
            <Th>Facts</Th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/60">
          {items.map((e) => (
            <tr
              key={e.slug}
              onClick={onSelect ? () => onSelect(e) : undefined}
              className={cn(
                'transition-colors duration-150',
                onSelect && 'cursor-pointer hover:bg-muted/40',
              )}
            >
              <td className="px-3 py-2 align-top text-foreground">{e.name}</td>
              <td className="px-3 py-2 align-top text-xs text-muted-foreground">{e.entity_type}</td>
              <td className="px-3 py-2 align-top text-xs text-muted-foreground">
                {e.classification}
              </td>
              <td className="px-3 py-2 align-top text-xs tabular-nums text-muted-foreground">
                {e.facts.length}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// --- Sources ----------------------------------------------------------------

function SourceDetail({
  agentId,
  entity,
  onOpenChange,
}: {
  agentId: string
  entity: EntityRecord | null
  onOpenChange: (o: boolean) => void
}) {
  const sourceId = entity ? stripSourcePrefix(entity.slug) : null
  const mapping = useSourceMapping(agentId, sourceId)
  if (entity == null) return null

  return (
    <Sheet open={entity != null} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-xl">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="text-sm">{entity.name}</SheetTitle>
          <SheetDescription>
            {entity.entity_type} · {entity.classification} · confidence{' '}
            <span className="font-mono tabular-nums">{entity.confidence.toFixed(2)}</span>
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 space-y-5 overflow-auto p-5">
          <section className="space-y-2">
            <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Routed homes
            </h3>
            <QueryState
              query={mapping}
              isEmpty={(d) => d.item == null || d.item.homes.length === 0}
              empty={<p className="text-xs text-muted-foreground">No committed home routing.</p>}
            >
              {(data) => (
                <div className="flex flex-wrap gap-1.5">
                  {(data.item?.homes ?? []).map((h) => (
                    <Chip key={h}>{h}</Chip>
                  ))}
                </div>
              )}
            </QueryState>
          </section>

          <section className="space-y-2">
            <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Facts{entity.facts.length ? ` (${entity.facts.length})` : ''}
            </h3>
            <FactList facts={entity.facts} />
          </section>
        </div>
      </SheetContent>
    </Sheet>
  )
}

function SourcesSection({ agentId }: { agentId: string }) {
  const sources = useSources(agentId)
  const [selected, setSelected] = useState<EntityRecord | null>(null)
  return (
    <div className="space-y-3">
      <QueryState
        query={sources}
        isEmpty={(d) => d.items.length === 0}
        empty={
          <EmptyState
            icon={<Plug className="size-5" />}
            title="No connected sources"
            description="This agent has no registered connector sources."
          />
        }
      >
        {(data) => <EntityTable items={data.items} onSelect={setSelected} />}
      </QueryState>
      <SourceDetail
        agentId={agentId}
        entity={selected}
        onOpenChange={(o) => !o && setSelected(null)}
      />
    </div>
  )
}

// --- Documents --------------------------------------------------------------

function DocumentsSection({ agentId }: { agentId: string }) {
  const [source, setSource] = useState('')
  const [q, setQ] = useState('')
  const docs = useDocuments(agentId, source, q)
  const ready = !!source && q.trim().length > 0

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <SourceSelect agentId={agentId} value={source} onChange={setSource} />
        <Input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search documents…"
          className="max-w-sm"
        />
      </div>
      {!ready ? (
        <EmptyState
          icon={<FileText className="size-5" />}
          title="Search a source"
          description="Pick a source and type a query to search its indexed documents."
        />
      ) : (
        <QueryState
          query={docs}
          isEmpty={(d) => d.items.length === 0}
          empty={<EmptyState title="No matching documents" />}
        >
          {(data) => (
            <ul className="space-y-2">
              {data.items.map((h) => (
                <li
                  key={h.chunk_id}
                  className="space-y-1.5 rounded-lg border border-border bg-muted/20 px-3 py-2"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <MonoChip>{h.pointer || h.chunk_id}</MonoChip>
                    <Chip>{h.classification}</Chip>
                    <span className="font-mono text-[11px] tabular-nums text-muted-foreground">
                      score {h.score.toFixed(3)}
                    </span>
                  </div>
                  <p className="text-sm text-foreground">{h.text}</p>
                  {h.provenance.length > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {h.provenance.map((p, i) => (
                        <Chip key={i}>{p}</Chip>
                      ))}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </QueryState>
      )}
    </div>
  )
}

// --- Datastore --------------------------------------------------------------

function DatastoreLookup({ agentId }: { agentId: string }) {
  const [source, setSource] = useState('')
  const [table, setTable] = useState('')
  const [op, setOp] = useState<DatastoreOp>('get_record')
  const [pkValue, setPkValue] = useState('')
  const [column, setColumn] = useState('')
  const [value, setValue] = useState('')
  const [limit, setLimit] = useState('')

  const args: Record<string, string> = {}
  if (op === 'get_record' && pkValue) args.pk_value = pkValue
  if (op === 'find') {
    if (column) args.column = column
    if (value) args.value = value
  }
  if (op !== 'get_record' && limit) args.limit = limit

  const result = useDatastoreQuery(agentId, source, op, table, args)

  return (
    <div className="space-y-3 rounded-lg border border-border bg-muted/20 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <SourceSelect agentId={agentId} value={source} onChange={setSource} />
        <Input
          value={table}
          onChange={(e) => setTable(e.target.value)}
          placeholder="table"
          className="w-40"
        />
        <Select value={op} onValueChange={(v) => setOp(v as DatastoreOp)}>
          <SelectTrigger className="w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="get_record">get_record</SelectItem>
            <SelectItem value="find">find</SelectItem>
            <SelectItem value="list">list</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {op === 'get_record' && (
          <Input
            value={pkValue}
            onChange={(e) => setPkValue(e.target.value)}
            placeholder="pk_value"
            className="w-40"
          />
        )}
        {op === 'find' && (
          <>
            <Input
              value={column}
              onChange={(e) => setColumn(e.target.value)}
              placeholder="column"
              className="w-40"
            />
            <Input
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder="value"
              className="w-40"
            />
          </>
        )}
        {op !== 'get_record' && (
          <Input
            value={limit}
            onChange={(e) => setLimit(e.target.value)}
            placeholder="limit"
            className="w-28"
          />
        )}
      </div>
      {!source || !table ? (
        <p className="text-xs text-muted-foreground">
          Pick a source and enter a table to run a live read.
        </p>
      ) : (
        <QueryState
          query={result}
          isEmpty={(d) => d.result == null}
          empty={<p className="text-xs text-muted-foreground">No rows returned.</p>}
        >
          {(data) => <JsonBlock value={data.result} className="max-h-96" />}
        </QueryState>
      )}
    </div>
  )
}

function DatastoreSection({ agentId }: { agentId: string }) {
  const tables = useDatastoreTables(agentId)
  return (
    <div className="space-y-4">
      <section className="space-y-2">
        <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          Tables
        </h3>
        <QueryState
          query={tables}
          isEmpty={(d) => d.items.length === 0}
          empty={
            <EmptyState
              icon={<Database className="size-5" />}
              title="No datastore tables"
              description="No connected datastore has been introspected for this agent."
            />
          }
        >
          {(data) => <EntityTable items={data.items} typeLabel="Type" />}
        </QueryState>
      </section>
      <section className="space-y-2">
        <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          Lookup
        </h3>
        <DatastoreLookup agentId={agentId} />
      </section>
    </div>
  )
}

// --- Blob folders -----------------------------------------------------------

function BlobSection({ agentId }: { agentId: string }) {
  const folders = useBlobFolders(agentId)
  return (
    <QueryState
      query={folders}
      isEmpty={(d) => d.items.length === 0}
      empty={
        <EmptyState
          icon={<FolderTree className="size-5" />}
          title="No blob folders"
          description="No source has been routed to blob storage for this agent."
        />
      }
    >
      {(data) => (
        <ul className="space-y-2">
          {data.items.map((f) => (
            <li
              key={f.slug}
              className="space-y-1.5 rounded-lg border border-border bg-muted/20 px-3 py-2"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm text-foreground">{f.name}</span>
                <Chip>{f.classification}</Chip>
              </div>
              <FactList facts={f.facts} />
            </li>
          ))}
        </ul>
      )}
    </QueryState>
  )
}

// --- Provenance -------------------------------------------------------------

function ProvenanceSection({ agentId }: { agentId: string }) {
  const [itemId, setItemId] = useState('')
  const trimmed = itemId.trim()
  const prov = useProvenance(agentId, trimmed || null)

  return (
    <div className="space-y-3">
      <Input
        value={itemId}
        onChange={(e) => setItemId(e.target.value)}
        placeholder="Canonical item id…"
        className="max-w-md"
      />
      {!trimmed ? (
        <EmptyState
          icon={<Waypoints className="size-5" />}
          title="Enter an item id"
          description="Type a canonical item id to see every source that claims it."
        />
      ) : (
        <QueryState
          query={prov}
          isEmpty={(d) => d.items.length === 0}
          empty={<EmptyState title="No provenance recorded" />}
        >
          {(data) => (
            <div className="overflow-hidden rounded-lg border border-border bg-card shadow-xs">
              <table className="w-full text-sm">
                <thead className="bg-muted/40">
                  <tr className="border-b border-border">
                    <Th>Source</Th>
                    <Th>External id</Th>
                    <Th>Classification</Th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border/60">
                  {data.items.map((p, i) => (
                    <tr key={i}>
                      <td className="px-3 py-2 align-top text-foreground">{p.source}</td>
                      <td className="px-3 py-2 align-top">
                        <MonoChip>{p.external_id || '—'}</MonoChip>
                      </td>
                      <td className="px-3 py-2 align-top text-xs text-muted-foreground">
                        {p.classification}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </QueryState>
      )}
    </div>
  )
}

// --- Index health -----------------------------------------------------------

function HealthTile({
  label,
  value,
  icon,
}: {
  label: string
  value: ReactNode
  icon: ReactNode
}) {
  return (
    <div className="flex items-center gap-2.5 rounded-lg border border-border bg-card px-3 py-2">
      <span className="shrink-0 text-muted-foreground/70">{icon}</span>
      <div className="min-w-0">
        <div className="font-display text-lg font-bold leading-none tabular-nums tracking-tight text-foreground">
          {value}
        </div>
        <div className="truncate text-[9px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          {label}
        </div>
      </div>
    </div>
  )
}

const yesNo = (b: boolean) => (b ? 'On' : 'Off')

function HealthSection({ agentId }: { agentId: string }) {
  const health = useIndexHealth(agentId)
  return (
    <QueryState query={health} isEmpty={(d) => d.item == null}>
      {(data) => {
        const s = data.item
        return (
          <div className="space-y-5">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <HealthTile
                label="Semantic recall"
                value={yesNo(s.live)}
                icon={<Activity className="size-4" />}
              />
              <HealthTile
                label="vec extension"
                value={yesNo(s.vec_extension)}
                icon={<Database className="size-4" />}
              />
              <HealthTile
                label={`Embedder (${s.embedder_backend})`}
                value={yesNo(s.embedder_live)}
                icon={<Plug className="size-4" />}
              />
              <HealthTile
                label="Embedding dims"
                value={s.embedder_dims ?? '—'}
                icon={<Waypoints className="size-4" />}
              />
            </div>

            {s.detail && <p className="text-xs text-muted-foreground">{s.detail}</p>}

            {s.degraded_reasons.length > 0 && (
              <section className="space-y-2">
                <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                  Degraded
                </h3>
                <div className="flex flex-wrap gap-1.5">
                  {s.degraded_reasons.map((r) => (
                    <span
                      key={r}
                      className="rounded-full border border-status-warning/30 bg-status-warning/15 px-2 py-0.5 text-xs text-status-warning"
                    >
                      {r}
                    </span>
                  ))}
                </div>
              </section>
            )}

            <section className="space-y-2">
              <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                Workspaces
              </h3>
              {s.workspaces.length === 0 ? (
                <p className="text-xs text-muted-foreground">No indexed workspace.</p>
              ) : (
                <div className="overflow-hidden rounded-lg border border-border bg-card shadow-xs">
                  <table className="w-full text-sm">
                    <thead className="bg-muted/40">
                      <tr className="border-b border-border">
                        <Th>Workspace</Th>
                        <Th>Indexed chunks</Th>
                        <Th>Embedded chunks</Th>
                        <Th>Insight triggers</Th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border/60">
                      {s.workspaces.map((w) => (
                        <tr key={w.workspace}>
                          <td className="max-w-xs truncate px-3 py-2 align-top font-mono text-xs text-muted-foreground">
                            {w.workspace}
                          </td>
                          <td className="px-3 py-2 align-top tabular-nums text-foreground">
                            {w.indexed_chunks}
                          </td>
                          <td className="px-3 py-2 align-top tabular-nums text-foreground">
                            {w.embedded_chunks}
                          </td>
                          <td className="px-3 py-2 align-top tabular-nums text-muted-foreground">
                            {w.insight_triggers}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </div>
        )
      }}
    </QueryState>
  )
}

// --- Browser shell ----------------------------------------------------------

/** The Connections offshoot of Knowledge (SPEC-073): read-only views of an
 *  agent's connected data sources — sources and their routing, indexed
 *  documents, connected datastores, blob folders, item provenance, and the
 *  honest index-health probe. */
export function ConnectionsBrowser({ agentId }: { agentId: string }) {
  const [section, setSection] = useState<Section>('sources')
  return (
    <Tabs value={section} onValueChange={(v) => setSection(v as Section)} className="space-y-4">
      <TabsList variant="line">
        {SECTIONS.map((s) => (
          <TabsTrigger key={s.value} value={s.value}>
            {s.label}
          </TabsTrigger>
        ))}
      </TabsList>
      <TabsContent value="sources">
        <SourcesSection agentId={agentId} />
      </TabsContent>
      <TabsContent value="documents">
        <DocumentsSection agentId={agentId} />
      </TabsContent>
      <TabsContent value="datastore">
        <DatastoreSection agentId={agentId} />
      </TabsContent>
      <TabsContent value="blob">
        <BlobSection agentId={agentId} />
      </TabsContent>
      <TabsContent value="provenance">
        <ProvenanceSection agentId={agentId} />
      </TabsContent>
      <TabsContent value="health">
        <HealthSection agentId={agentId} />
      </TabsContent>
    </Tabs>
  )
}
