import { useState, type ReactNode } from 'react'
import {
  Activity,
  Database,
  FileText,
  FolderTree,
  Pause,
  Play,
  Plug,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  ShieldX,
  Trash2,
  TriangleAlert,
  Waypoints,
} from 'lucide-react'
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
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { JsonBlock } from '@/components/json-block'
import { EmptyState, QueryState } from '@/components/states'
import {
  useBlobFolders,
  useConnectedSourceAction,
  useConnectedSources,
  useDatastoreQuery,
  useDatastoreTables,
  useDocuments,
  useIndexHealth,
  useProvenance,
  useMappingProposal,
  useResolveApproval,
  useResolveProfileReview,
  useConnectedResources,
  useProfileReviews,
  useSelectConnectedResources,
  useStageSourceMapping,
} from '@/lib/queries'
import { cn } from '@/lib/utils'
import type { ConnectedSourceItem, EntityRecord } from '@/lib/types'

const SECTIONS = [
  { value: 'sources', label: 'Sources' },
  { value: 'documents', label: 'Documents' },
  { value: 'datastore', label: 'Datastore' },
  { value: 'blob', label: 'Blob folders' },
  { value: 'provenance', label: 'Provenance' },
  { value: 'reviews', label: 'Profile review' },
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
  const sources = useConnectedSources(agentId)
  const items = sources.data?.items ?? []
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className="w-56">
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        {items.map((s) => (
          <SelectItem key={s.connection_id} value={s.source_id}>
            {s.label || s.connection_id}
          </SelectItem>
        ))}
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

const MAP_HOMES = [
  { value: 'document', label: 'Document / RAG' },
  { value: 'memory', label: 'Memory' },
  { value: 'profile', label: 'User profile' },
  { value: 'blob', label: 'Blob storage' },
  { value: 'datastore', label: 'Datastore / SQL' },
] as const

function sourceStatusTone(status: string): string {
  if (status === 'complete' || status === 'idle') return 'border-status-success/30 bg-status-success/10 text-status-success'
  if (status === 'failed' || status === 'degraded') return 'border-destructive/30 bg-destructive/10 text-destructive'
  if (status === 'awaiting_mapping') return 'border-status-warning/30 bg-status-warning/15 text-status-warning'
  return 'border-border bg-muted/40 text-muted-foreground'
}

function SourceControls({ agentId, source }: { agentId: string; source: ConnectedSourceItem }) {
  const action = useConnectedSourceAction(agentId, source.connection_id)
  const busy = action.isPending
  const run = (verb: string) => action.mutate(verb)
  const paused = source.status === 'paused'
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="outline" disabled={busy || paused} onClick={() => run('sync')}>
          <Play className="size-3.5" /> Initial sync
        </Button>
        <Button size="sm" variant="outline" disabled={busy || paused} onClick={() => run('retry')}>
          <RotateCcw className="size-3.5" /> Retry
        </Button>
        <Button size="sm" variant="outline" disabled={busy} onClick={() => run(paused ? 'resume' : 'pause')}>
          {paused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
          {paused ? 'Resume' : 'Pause'}
        </Button>
        <Button size="sm" variant="outline" disabled={busy || paused} onClick={() => run('reindex')}>
          <RefreshCw className="size-3.5" /> Reindex
        </Button>
        <Button size="sm" variant="destructive" disabled={busy} onClick={() => run('revoke')}>
          <Trash2 className="size-3.5" /> Revoke
        </Button>
      </div>
      {action.isError && (
        <p role="alert" className="flex items-center gap-1.5 text-xs text-destructive">
          <TriangleAlert className="size-3.5" /> {action.error.message}
        </p>
      )}
    </div>
  )
}

function SourceMappingControl({ agentId, source }: { agentId: string; source: ConnectedSourceItem }) {
  const proposal = useMappingProposal(agentId, source.connection_id)
  const stage = useStageSourceMapping(agentId, source.connection_id)
  const resolve = useResolveApproval(agentId, source.connection_id)
  const currentHomes = proposal.data?.item?.homes ?? []
  const allowedHomes = proposal.data?.item?.allowed_homes ?? source.allowed_homes
  const [homes, setHomes] = useState<string[]>([])
  const selected = homes.length > 0 ? homes : currentHomes
  const status = proposal.data?.item?.status
  const approvalId = proposal.data?.item?.approval_id
  const toggleHome = (home: string) =>
    setHomes((prior) => {
      const current = prior.length > 0 ? prior : currentHomes
      return current.includes(home)
        ? current.filter((value) => value !== home)
        : [...current, home]
    })

  return (
    <section className="space-y-3">
      <div>
        <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          Agent data mapping
        </h3>
        <p className="mt-1 text-xs text-muted-foreground">
          Select how this account may improve the agent. Mapping stays inactive until a signed operator approval.
        </p>
      </div>
      <QueryState query={proposal}>
        {() => (
          <>
            <div className="grid gap-2 sm:grid-cols-2">
              {MAP_HOMES.filter((home) => allowedHomes.length === 0 || allowedHomes.includes(home.value)).map((home) => {
                const checked = selected.includes(home.value)
                return (
                  <label
                    key={home.value}
                    className="flex cursor-pointer items-center gap-2 rounded-md border border-border bg-muted/20 px-3 py-2 text-sm"
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleHome(home.value)}
                      className="size-4 accent-primary"
                    />
                    {home.label}
                  </label>
                )
              })}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                disabled={selected.length === 0 || stage.isPending}
                onClick={() => stage.mutate(selected)}
              >
                <ShieldCheck className="size-3.5" /> Request mapping approval
              </Button>
              {status && <Chip>{status}</Chip>}
              {approvalId && status === 'pending' && (
                <>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={resolve.isPending}
                    onClick={() => resolve.mutate({ approvalId, decision: 'approve' })}
                  >
                    <ShieldCheck className="size-3.5" /> Approve mapping
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={resolve.isPending}
                    onClick={() => resolve.mutate({ approvalId, decision: 'deny' })}
                  >
                    <ShieldX className="size-3.5" /> Deny mapping
                  </Button>
                </>
              )}
            </div>
            {(stage.isError || resolve.isError) && (() => {
              const error = stage.error ?? resolve.error
              return error ? (
              <p role="alert" className="flex items-center gap-1.5 text-xs text-destructive">
                <ShieldX className="size-3.5" /> {error.message}
              </p>
              ) : null
            })()}
          </>
        )}
      </QueryState>
    </section>
  )
}

function SourceResourceControl({ agentId, source }: { agentId: string; source: ConnectedSourceItem }) {
  const resources = useConnectedResources(agentId, source.connection_id)
  const select = useSelectConnectedResources(agentId, source.connection_id)
  const [chosen, setChosen] = useState<string[] | null>(null)
  const selected = chosen ?? (resources.data?.items.filter((item) => item.selected).map((item) => item.resource_id) ?? [])
  const toggle = (resourceId: string) =>
    setChosen((previous) => {
      const current = previous ?? selected
      return current.includes(resourceId)
        ? current.filter((item) => item !== resourceId)
        : [...current, resourceId]
    })

  return (
    <section className="space-y-3">
      <div>
        <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          Connected account scope
        </h3>
        <p className="mt-1 text-xs text-muted-foreground">
          Choose folders, mailboxes, or labels before sync. Only selected resources can be ingested.
        </p>
      </div>
      <QueryState query={resources} isEmpty={(data) => data.items.length === 0} empty={<p className="text-xs text-muted-foreground">This connector has no selectable resources.</p>}>
        {(data) => (
          <>
            <div className="max-h-48 space-y-1 overflow-auto rounded-md border border-border p-2">
              {data.items.map((resource) => (
                <label key={resource.resource_id} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-muted/50">
                  <input type="checkbox" checked={selected.includes(resource.resource_id)} onChange={() => toggle(resource.resource_id)} className="size-4 accent-primary" />
                  <span className="min-w-0 flex-1 truncate">{resource.label}</span>
                  <Chip>{resource.resource_kind}</Chip>
                </label>
              ))}
            </div>
            <Button size="sm" variant="outline" disabled={selected.length === 0 || select.isPending} onClick={() => select.mutate(selected, { onSuccess: () => setChosen(null) })}>
              Save selected resources
            </Button>
            {select.isError && <p role="alert" className="text-xs text-destructive">{select.error.message}</p>}
          </>
        )}
      </QueryState>
    </section>
  )
}

function SourceDetail({
  agentId,
  source,
  onOpenChange,
}: {
  agentId: string
  source: ConnectedSourceItem | null
  onOpenChange: (o: boolean) => void
}) {
  if (source == null) return null

  return (
    <Sheet open={source != null} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-hidden p-0 sm:max-w-xl">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="text-sm">{source.label || source.connection_id}</SheetTitle>
          <SheetDescription>
            {source.source_kind} · <span className="font-mono">{source.connection_id}</span>
          </SheetDescription>
        </SheetHeader>
        <div className="flex-1 space-y-5 overflow-auto p-5">
          <section className="space-y-2">
            <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
              Synchronization
            </h3>
            <dl className="grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
              <dt className="text-muted-foreground">Status</dt><dd className="font-medium">{source.status}</dd>
              <dt className="text-muted-foreground">Pages</dt><dd>{source.pages}</dd>
              <dt className="text-muted-foreground">Processed</dt><dd>{source.bytes_processed.toLocaleString()} bytes</dd>
              <dt className="text-muted-foreground">Last sync</dt><dd>{source.last_synced_at ?? 'Never'}</dd>
              {source.error_code && <><dt className="text-muted-foreground">Error</dt><dd className="text-destructive">{source.error_code}</dd></>}
              {source.detail && <><dt className="text-muted-foreground">Detail</dt><dd>{source.detail}</dd></>}
            </dl>
          </section>
          <SourceResourceControl agentId={agentId} source={source} />
          <SourceMappingControl agentId={agentId} source={source} />
          <section className="space-y-2">
            <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Controls</h3>
            <SourceControls agentId={agentId} source={source} />
          </section>
        </div>
      </SheetContent>
    </Sheet>
  )
}

function SourcesSection({ agentId }: { agentId: string }) {
  const sources = useConnectedSources(agentId)
  const [selected, setSelected] = useState<ConnectedSourceItem | null>(null)
  return (
    <div className="space-y-3">
      <QueryState
        query={sources}
        isEmpty={(d) => d.items.length === 0}
        empty={
          <EmptyState
            icon={<Plug className="size-5" />}
            title="No connected sources"
            description="Connect an account, then choose how its information should be mapped for this agent."
          />
        }
      >
        {(data) => (
          <div className="overflow-hidden rounded-lg border border-border bg-card shadow-xs">
            <table className="w-full text-sm">
              <thead className="bg-muted/40"><tr className="border-b border-border"><Th>Source</Th><Th>Type</Th><Th>Status</Th><Th>Progress</Th><Th>Last sync</Th></tr></thead>
              <tbody className="divide-y divide-border/60">
                {data.items.map((source) => (
                  <tr key={source.connection_id} onClick={() => setSelected(source)} className="cursor-pointer transition-colors hover:bg-muted/40">
                    <td className="px-3 py-2 text-foreground">{source.label || source.connection_id}</td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">{source.source_kind}</td>
                    <td className="px-3 py-2"><span className={`rounded-full border px-2 py-0.5 text-xs ${sourceStatusTone(source.status)}`}>{source.status}</span></td>
                    <td className="px-3 py-2 text-xs tabular-nums text-muted-foreground">{source.pages} pages · {source.bytes_processed.toLocaleString()} B</td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">{source.last_synced_at ?? 'Never'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </QueryState>
      <SourceDetail
        agentId={agentId}
        source={selected}
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
  const [source, setSource] = useState('')
  const [query, setQuery] = useState('')
  const folders = useBlobFolders(agentId, source || undefined)
  const visible = (folders.data?.items ?? []).filter((folder) =>
    `${folder.name} ${folder.facts.join(' ')}`.toLowerCase().includes(query.trim().toLowerCase()),
  )
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <SourceSelect agentId={agentId} value={source} onChange={setSource} placeholder="All sources" />
        <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter folders and objects…" className="max-w-sm" />
      </div>
      <QueryState
        query={folders}
        isEmpty={() => visible.length === 0}
        empty={
          <EmptyState
            icon={<FolderTree className="size-5" />}
            title={query ? 'No matching blob folders' : 'No blob folders'}
            description="Choose a source mapped to blob storage, then browse its indexed folder inventory."
          />
        }
      >
        {() => (
          <ul className="space-y-2">
            {visible.map((f) => (
              <li key={f.slug} className="space-y-1.5 rounded-lg border border-border bg-muted/20 px-3 py-2">
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
    </div>
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

// --- Profile review ---------------------------------------------------------

function ProfileReviewSection({ agentId }: { agentId: string }) {
  const [status, setStatus] = useState('pending')
  const [source, setSource] = useState('')
  const reviews = useProfileReviews(agentId, status, source)
  const resolve = useResolveProfileReview(agentId)

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Select value={status} onValueChange={setStatus}>
          <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="pending">Pending review</SelectItem>
            <SelectItem value="approved">Approved profile</SelectItem>
            <SelectItem value="declined">Declined</SelectItem>
            <SelectItem value="undone">Undone</SelectItem>
          </SelectContent>
        </Select>
        <SourceSelect agentId={agentId} value={source} onChange={setSource} placeholder="All sources" />
      </div>
      <p className="text-xs text-muted-foreground">
        Inferred profile facts never enter agent context until you approve them. Approved facts are the only profile facts agents can recall.
      </p>
      <QueryState
        query={reviews}
        isEmpty={(data) => data.items.length === 0}
        empty={<EmptyState title={status === 'pending' ? 'No profile facts awaiting review' : 'No profile facts in this state'} />}
      >
        {(data) => (
          <ul className="space-y-2">
            {data.items.map((item) => (
              <li key={item.fact_id} className="space-y-2 rounded-lg border border-border bg-muted/20 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-foreground">{item.field}</span>
                  <Chip>{item.kind}</Chip><Chip>{item.classification}</Chip><Chip>{item.status}</Chip>
                  <span className="font-mono text-[11px] text-muted-foreground">{item.source_id}</span>
                </div>
                <p className="text-sm text-foreground">{item.value}</p>
                <div className="flex flex-wrap gap-2">
                  {item.status === 'pending' && (
                    <>
                      <Button size="sm" disabled={resolve.isPending} onClick={() => resolve.mutate({ factId: item.fact_id, decision: 'approve' })}>
                        <ShieldCheck className="size-3.5" /> Approve
                      </Button>
                      <Button size="sm" variant="outline" disabled={resolve.isPending} onClick={() => resolve.mutate({ factId: item.fact_id, decision: 'decline' })}>
                        <ShieldX className="size-3.5" /> Decline
                      </Button>
                    </>
                  )}
                  {item.status === 'approved' && (
                    <Button size="sm" variant="outline" disabled={resolve.isPending} onClick={() => resolve.mutate({ factId: item.fact_id, decision: 'undo' })}>
                      <RotateCcw className="size-3.5" /> Undo approval
                    </Button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </QueryState>
      {resolve.isError && <p role="alert" className="text-xs text-destructive">{resolve.error.message}</p>}
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
      <TabsContent value="reviews">
        <ProfileReviewSection agentId={agentId} />
      </TabsContent>
      <TabsContent value="health">
        <HealthSection agentId={agentId} />
      </TabsContent>
    </Tabs>
  )
}
