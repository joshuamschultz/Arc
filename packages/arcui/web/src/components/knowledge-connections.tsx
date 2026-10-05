import { useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  Activity,
  Database,
  FolderTree,
  Library,
  LogIn,
  MoveRight,
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
import { FieldHelp } from '@/components/help'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { JsonBlock } from '@/components/json-block'
import { GuidesSection } from '@/components/connection-guides'
import { SyncWorkerStatusLine } from '@/components/sync-worker-status'
import { Chip, MonoChip, SourceSelect, Th } from '@/components/knowledge-connection-bits'
import { isDatastoreKind } from '@/lib/connection-kind'
import { fmtBytes } from '@/lib/format'
import { EmptyState, QueryState } from '@/components/states'
import { ApiError } from '@/lib/api'
import {
  useActivateConnectedData,
  useBlobFolders,
  useConnectedSourceAction,
  useConnectedSources,
  useConnectionChunks,
  useConnectionChunkSearch,
  useConnectionTables,
  useDatastoreQuery,
  useDocuments,
  useSourceIndex,
  useIndexHealth,
  useProvenance,
  useMappingProposal,
  useResolveApproval,
  useConnectedResources,
  useSelectConnectedResources,
  useStageSourceMapping,
  usePreviewSharedMigration,
} from '@/lib/queries'
import { cn } from '@/lib/utils'
import type {
  ChunkPage,
  ChunkSearchMode,
  ChunkSearchResponse,
  CollectionIndexView,
  ConnectedSourceItem,
  EntityRecord,
} from '@/lib/types'

const SECTIONS = [
  { value: 'sources', label: 'Sources' },
  { value: 'browse', label: 'Browse' },
  { value: 'guides', label: 'Guides' },
] as const

type Section = (typeof SECTIONS)[number]['value']
type DatastoreOp = 'get_record' | 'find' | 'list'

const SECTION_HEADING = 'text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground'

// --- Shared bits ------------------------------------------------------------

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
    <div className="overflow-x-auto rounded-lg border border-border bg-card shadow-xs">
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

/** Failure codes that only the account's owner can fix by connecting it again. */
const SIGNED_OUT_CODES = new Set([
  'auth_required',
  'invalid_grant',
  'consent_required',
  'interaction_required',
  'credential_missing',
  'credential_unreadable',
])

/** What went wrong, in words a person can act on. Never the raw code or exception text. */
function sourceProblem(source: ConnectedSourceItem): { text: string; short: string; reconnect: boolean } | null {
  const code = source.error_code ?? (source.status === 'needs_attention' ? source.detail : '')
  if (SIGNED_OUT_CODES.has(code ?? '') || SIGNED_OUT_CODES.has(source.detail)) {
    return { text: 'This account is signed out. Reconnect it so Arc can read it again.', short: 'Signed out', reconnect: true }
  }
  if (code === 'rate_limited' || source.detail === 'rate_limited') {
    return { text: 'The provider asked Arc to slow down. The next sync starts on its own.', short: 'Waiting on the provider', reconnect: false }
  }
  if (code === 'repeated_failures' || source.status === 'needs_attention') {
    return {
      text: 'Syncing failed several times in a row, so Arc stopped trying. Try again, or reconnect the account if it keeps failing.',
      short: 'Sync keeps failing',
      reconnect: true,
    }
  }
  if (code === 'interrupted') {
    return { text: 'The last sync stopped when Arc restarted. It continues from where it stopped.', short: 'Resuming after a restart', reconnect: false }
  }
  if (source.detail === 'source_inspection_failed') {
    return { text: 'Arc could not reach this account just now. It tries again on its own.', short: 'Could not reach the account', reconnect: false }
  }
  if (source.status === 'failed' || source.status === 'degraded') {
    return { text: 'The last sync did not finish. Arc tries again on its own.', short: 'Last sync did not finish', reconnect: false }
  }
  return null
}

const LANE_LABELS: Record<ConnectedSourceItem['lane'], string> = {
  own: 'Own copy',
  migrating: 'Own copy, waiting to move to the shared store',
  shared: 'Shared store',
}

/** Reconnecting happens on the Connections page, where each account signs in. */
function ReconnectLink() {
  return (
    <Button asChild size="sm" variant="outline">
      <Link to="/connections">
        <LogIn className="size-3.5" /> Reconnect
      </Link>
    </Button>
  )
}

function SourceProblem({ source }: { source: ConnectedSourceItem }) {
  const problem = sourceProblem(source)
  if (problem == null) return null
  return (
    <div role="status" className="space-y-2 rounded-md border border-status-warning/30 bg-status-warning/10 px-3 py-2 text-xs">
      <p className="flex items-start gap-1.5">
        <TriangleAlert className="mt-0.5 size-3.5 shrink-0" /> {problem.text}
      </p>
      {problem.reconnect && <ReconnectLink />}
    </div>
  )
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
        <Button
          size="sm"
          variant="outline"
          disabled={busy || paused}
          title="Rebuild this source's folders and routing index from what is already stored"
          onClick={() => run('relayout')}
        >
          <FolderTree className="size-3.5" /> Relayout
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

  const row = (resource: { resource_id: string; label: string; resource_kind: string }) => (
    <label key={resource.resource_id} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-muted/50">
      <input type="checkbox" checked={selected.includes(resource.resource_id)} onChange={() => toggle(resource.resource_id)} className="size-4 accent-primary" />
      <span className="min-w-0 flex-1 truncate">{resource.label}</span>
      <Chip>{resource.resource_kind === "all" ? "category" : resource.resource_kind}</Chip>
    </label>
  )

  return (
    <section className="space-y-3">
      <div>
        <h3 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          Connected account scope
        </h3>
        <p className="mt-1 text-xs text-muted-foreground">
          Pick a whole category to sync everything of that kind, including ones created later, or choose individual resources. Only selected resources can be ingested.
        </p>
      </div>
      {resources.isError ? (
        <ResourceListProblem error={resources.error} onRetry={() => void resources.refetch()} />
      ) : (
      <QueryState query={resources} isEmpty={(data) => data.items.length === 0} empty={<p className="text-xs text-muted-foreground">This connector has no selectable resources.</p>}>
        {(data) => {
          const categories = data.items.filter((item) => item.resource_kind === "all")
          const individual = data.items.filter((item) => item.resource_kind !== "all")
          return (
            <>
              <div className="max-h-48 space-y-1 overflow-auto rounded-md border border-border p-2">
                {categories.length > 0 && (
                  <>
                    <p className="px-2 pb-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">Whole categories</p>
                    {categories.map(row)}
                    {individual.length > 0 && <div className="my-1 border-t border-border" />}
                  </>
                )}
                {individual.map(row)}
              </div>
              <Button size="sm" variant="outline" disabled={selected.length === 0 || select.isPending} onClick={() => select.mutate(selected, { onSuccess: () => setChosen(null) })}>
                Save selected resources
              </Button>
              {select.isError && <ResourceListProblem error={select.error} />}
            </>
          )
        }}
      </QueryState>
      )}
    </section>
  )
}

/** A resource list or selection that failed, said plainly with the step that fixes it. */
function ResourceListProblem({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  if (error instanceof ApiError && error.status === 409 && error.body?.action === 'reconnect') {
    return (
      <div role="alert" className="space-y-2 text-xs">
        <p>{error.message}</p>
        <ReconnectLink />
      </div>
    )
  }
  if (error instanceof ApiError && error.status === 503) {
    return (
      <div role="alert" className="space-y-2 text-xs">
        <p>The provider did not answer. This is usually brief.</p>
        {onRetry && (
          <Button size="sm" variant="outline" onClick={onRetry}>
            <RotateCcw className="size-3.5" /> Try again
          </Button>
        )}
      </div>
    )
  }
  return (
    <p role="alert" className="text-xs text-destructive">
      {error.message}
    </p>
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
              <dt className="text-muted-foreground">Batches read</dt><dd>{source.pages}</dd>
              <dt className="text-muted-foreground">Downloaded</dt><dd>{fmtBytes(source.bytes_processed)}</dd>
              <dt className="text-muted-foreground">Documents indexed</dt><dd>{source.documents_indexed}</dd>
              <dt className="text-muted-foreground">Last sync</dt><dd>{source.last_synced_at ?? 'Never'}</dd>
              <dt className="text-muted-foreground">Stored in</dt><dd>{LANE_LABELS[source.lane]}</dd>
            </dl>
            <SourceProblem source={source} />
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

function SourcesSection({
  agentId,
  initialConnectionId,
}: {
  agentId: string
  initialConnectionId?: string | null
}) {
  const sources = useConnectedSources(agentId)
  const [selected, setSelected] = useState<ConnectedSourceItem | null>(null)
  const [initialOpen, setInitialOpen] = useState(true)
  const initialSource = sources.data?.items.find(
    (source) =>
      source.connection_id === initialConnectionId ||
      source.connection_id.startsWith(`${initialConnectionId}:`),
  )
  const openedSource = selected ?? (initialOpen ? initialSource : null) ?? null
  if (sources.data?.status === 'degraded') {
    return <ActivateKnowledgeSync agentId={agentId} />
  }
  return (
    <div className="space-y-3">
      <HealthStrip agentId={agentId} />
      <SharedMigrationPreview agentId={agentId} />
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
          <div className="overflow-x-auto rounded-lg border border-border bg-card shadow-xs">
            <table className="w-full text-sm">
              <thead className="bg-muted/40"><tr className="border-b border-border"><Th>Source</Th><Th>Type</Th><Th>Status</Th><Th>Stored in</Th><Th>Progress</Th><Th>Last sync</Th></tr></thead>
              <tbody className="divide-y divide-border/60">
                {data.items.map((source) => (
                  <tr key={source.connection_id} onClick={() => setSelected(source)} className="cursor-pointer transition-colors hover:bg-muted/40">
                    <td className="px-3 py-2 text-foreground">
                      <div>{source.label || source.connection_id}</div>
                      {sourceProblem(source) && (
                        <p className="mt-0.5 max-w-xs text-xs text-status-warning">{sourceProblem(source)?.short}</p>
                      )}
                    </td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">{source.source_kind}</td>
                    <td className="px-3 py-2"><span className={`rounded-full border px-2 py-0.5 text-xs ${sourceStatusTone(source.status)}`}>{source.status}</span></td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">{LANE_LABELS[source.lane]}</td>
                    <td className="px-3 py-2 text-xs tabular-nums text-muted-foreground">{source.documents_indexed} documents · {source.pages} batches · {fmtBytes(source.bytes_processed)} downloaded</td>
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
        source={openedSource}
        onOpenChange={(open) => {
          if (!open) {
            setSelected(null)
            setInitialOpen(false)
          }
        }}
      />
    </div>
  )
}

/** The module is installed but not turned on for this agent: one click turns it on. */
function ActivateKnowledgeSync({ agentId }: { agentId: string }) {
  const activate = useActivateConnectedData(agentId)
  return (
    <div className="space-y-3">
      <EmptyState
        icon={<Plug className="size-5" />}
        title="Knowledge sync is off for this agent"
        description="The agent can use its connected tools, but it does not learn from them yet. Turn Knowledge sync on to let it read what you choose."
      />
      <div className="flex flex-col items-center gap-2">
        <Button size="sm" disabled={activate.isPending || activate.isSuccess} onClick={() => activate.mutate()}>
          <Play className="size-3.5" /> {activate.isPending ? 'Turning on…' : 'Turn on Knowledge sync'}
        </Button>
        {activate.isSuccess && <p className="text-xs text-status-success">Knowledge sync is on.</p>}
        {activate.isError && (
          <p role="alert" className="text-xs text-destructive">
            Knowledge sync could not be turned on. Try again in a moment.
          </p>
        )}
      </div>
    </div>
  )
}

const MIGRATION_OUTCOMES: Record<string, (documents: number) => string> = {
  would_migrate: (documents) => `Will move ${documents} documents into the shared store`,
  migrated: (documents) => `Moved ${documents} documents`,
  already_shared: () => 'Already in the shared store',
  nothing_to_migrate: () => 'Nothing to move',
  not_eligible: () => 'Stays as its own copy',
  refused: () => 'Cannot move yet. It stays as its own copy.',
}

/** What the automatic move into shared stores will do. It runs inside the next sync;
 *  the preview only reports it and changes nothing. */
function SharedMigrationPreview({ agentId }: { agentId: string }) {
  const preview = usePreviewSharedMigration(agentId)
  return (
    <section className="space-y-2 rounded-lg border border-border bg-card/40 px-3 py-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          Each account is stored once and shared by every agent granted it. An agent's own copy moves on its next sync.
        </p>
        <Button size="sm" variant="outline" disabled={preview.isPending} onClick={() => preview.mutate()}>
          <MoveRight className="size-3.5" /> Preview the move
        </Button>
      </div>
      {preview.isError && (
        <p role="alert" className="text-xs text-destructive">
          The preview is not available right now. Try again in a moment.
        </p>
      )}
      {preview.data && (
        <ul className="space-y-1 text-xs">
          {preview.data.items.length === 0 && <li className="text-muted-foreground">No connections to move.</li>}
          {preview.data.items.map((item) => (
            <li key={item.connection_id} className="flex justify-between gap-2">
              <span className="font-mono">{item.connection_id}</span>
              <span>{(MIGRATION_OUTCOMES[item.status] ?? (() => 'Stays as its own copy'))(item.adopted || item.documents)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

// --- Index health strip -----------------------------------------------------

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
    <div className="flex min-w-0 items-center gap-2.5 rounded-lg border border-border bg-card px-3 py-2">
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

/** The honest index probe, as a compact strip above the sources: is search live,
 *  how much is indexed, and what is degraded. */
function HealthStrip({ agentId }: { agentId: string }) {
  const health = useIndexHealth(agentId)
  return (
    <QueryState query={health} isEmpty={(d) => d.item == null}>
      {(data) => {
        const s = data.item
        const indexed = s.workspaces.reduce((sum, w) => sum + w.indexed_chunks, 0)
        const embedded = s.workspaces.reduce((sum, w) => sum + w.embedded_chunks, 0)
        return (
          <section aria-label="Index health" className="space-y-2">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <HealthTile label="Search by meaning" value={yesNo(s.live)} icon={<Activity className="size-4" />} />
              <HealthTile
                label={`Embedder (${s.embedder_backend})`}
                value={yesNo(s.embedder_live)}
                icon={<Plug className="size-4" />}
              />
              <HealthTile label="Chunks indexed" value={indexed} icon={<Database className="size-4" />} />
              <HealthTile label="Chunks embedded" value={embedded} icon={<Waypoints className="size-4" />} />
            </div>
            {s.detail && <p className="text-xs text-muted-foreground">{s.detail}</p>}
            {s.degraded_reasons.length > 0 && (
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
            )}
          </section>
        )
      }}
    </QueryState>
  )
}

// --- Browse: a file or document source --------------------------------------

/** The source's repository index: what it holds and what it is for. The server
 *  verifies the index fail-closed and only sends a body when it is trusted; an
 *  unverified index shows a tamper notice, never its contents. */
function IndexSummary({ data }: { data: CollectionIndexView }) {
  if (!data.verified) {
    return (
      <div role="alert" className="flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2.5">
        <ShieldX className="mt-0.5 size-4 shrink-0 text-destructive" />
        <div className="min-w-0 space-y-0.5">
          <p className="text-sm font-medium text-foreground">
            Index could not be verified. {data.guidance ?? 'Re-sync this source to restore its index.'}
          </p>
          <p className="text-xs text-muted-foreground">
            The index failed its check and is not shown, so an edit made outside Arc is never read as trusted knowledge.
            {data.error ? ` Reason: ${data.error}.` : ''}
          </p>
        </div>
      </div>
    )
  }
  const topics = data.entries.slice(0, 6).map((e) => e.title)
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <Chip>
          {data.document_count} document{data.document_count === 1 ? '' : 's'}
        </Chip>
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
          <ShieldCheck className="size-3.5 text-emerald-500" />
          verified
        </span>
      </div>
      {topics.length > 0 && (
        <p className="text-sm text-foreground">
          This source holds: {topics.join(', ')}
          {data.entries.length > topics.length ? ', and more.' : '.'}
        </p>
      )}
    </div>
  )
}

function IndexEntries({ data, onOpenFolder }: { data: CollectionIndexView; onOpenFolder: (path: string) => void }) {
  return (
    <ul className="space-y-2">
      {data.entries.map((entry) => (
        <li key={entry.path} className="min-w-0 space-y-1 rounded-lg border border-border bg-muted/20 px-3 py-2">
          <div className="flex flex-wrap items-center gap-2">
            {entry.kind === 'folder' ? (
              <button
                type="button"
                className="text-left text-sm font-medium text-foreground underline-offset-2 hover:underline"
                onClick={() => onOpenFolder(entry.path)}
              >
                {entry.title}
              </button>
            ) : (
              <span className="text-sm font-medium text-foreground">{entry.title}</span>
            )}
            {entry.kind === 'folder' ? (
              <Chip>
                {entry.count} document{entry.count === 1 ? '' : 's'}
              </Chip>
            ) : (
              <MonoChip>{entry.path}</MonoChip>
            )}
          </div>
          {entry.summary && <p className="text-sm text-muted-foreground">{entry.summary}</p>}
        </li>
      ))}
    </ul>
  )
}

/** One view of a file or document source: what it is for, its folders, and its
 *  documents with a search box. */
function FileSourceBrowse({ agentId, source }: { agentId: string; source: string }) {
  const [q, setQ] = useState('')
  // A source mirrors its remote tree: the root lists folders, each opened in place.
  const [folder, setFolder] = useState('')
  const index = useSourceIndex(agentId, source, folder)
  const docs = useDocuments(agentId, source, q)
  const blob = useBlobFolders(agentId, source)
  const parent = folder.includes('/') ? folder.slice(0, folder.lastIndexOf('/')) : ''
  const needle = q.trim().toLowerCase()
  const blobFolders = (blob.data?.items ?? []).filter((f) =>
    `${f.name} ${f.facts.join(' ')}`.toLowerCase().includes(needle),
  )
  const entries = index.data?.present && index.data.verified ? index.data.entries : []
  const noFolders = entries.length === 0 && blobFolders.length === 0

  return (
    <div className="space-y-6">
      <section className="space-y-2">
        <h3 className={SECTION_HEADING}>What this source is for</h3>
        <QueryState
          query={index}
          isEmpty={(d) => !d.present}
          empty={<p className="text-xs text-muted-foreground">This source has not written an index yet. Run a sync from the Sources tab.</p>}
        >
          {(data) => <IndexSummary data={data} />}
        </QueryState>
      </section>

      <section className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className={SECTION_HEADING}>Folders</h3>
          {folder && (
            <>
              <MonoChip>{folder}/</MonoChip>
              <Button size="sm" variant="ghost" onClick={() => setFolder(parent)}>
                Up
              </Button>
            </>
          )}
        </div>
        {index.data?.present && index.data.verified && <IndexEntries data={index.data} onOpenFolder={setFolder} />}
        {blobFolders.length > 0 && (
          <ul className="space-y-2">
            {blobFolders.map((f) => (
              <li key={f.slug} className="min-w-0 space-y-1.5 rounded-lg border border-border bg-muted/20 px-3 py-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm text-foreground">{f.name}</span>
                  <Chip>{f.classification}</Chip>
                </div>
                <FactList facts={f.facts} />
              </li>
            ))}
          </ul>
        )}
        {noFolders && <p className="text-xs text-muted-foreground">No folders are indexed for this source yet.</p>}
      </section>

      <section className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className={SECTION_HEADING}>Documents</h3>
          <Input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search documents and folders…"
            aria-label="Search documents and folders"
            className="w-full max-w-sm"
          />
          <FieldHelp helpKey="knowledge.resource_filter" route="knowledge" />
        </div>
        <QueryState
          query={docs}
          isEmpty={(d) => d.items.length === 0}
          empty={
            <p className="text-xs text-muted-foreground">
              {q.trim() ? 'No matching documents.' : 'This source has not written any documents. Run a sync from the Sources tab.'}
            </p>
          }
        >
          {(data) => (
            <ul className="space-y-2">
              {data.items.map((h) => (
                <li key={h.chunk_id} className="min-w-0 space-y-1.5 rounded-lg border border-border bg-muted/20 px-3 py-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <MonoChip>{h.pointer || h.chunk_id}</MonoChip>
                    <Chip>{h.classification}</Chip>
                    {q.trim().length > 0 && (
                      <span className="font-mono text-[11px] tabular-nums text-muted-foreground">score {h.score.toFixed(3)}</span>
                    )}
                  </div>
                  <p className="break-words text-sm text-foreground">{h.text}</p>
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
      </section>
    </div>
  )
}

// --- Browse: a datastore source ---------------------------------------------

const LOOKUP_OPERATIONS: Array<{ value: DatastoreOp; label: string }> = [
  { value: 'get_record', label: 'Get one record by id' },
  { value: 'find', label: 'Find records by value' },
  { value: 'list', label: 'List records' },
]

/** A live read of one record, row set, or listing from the picked datastore. */
function RecordLookup({ agentId, source }: { agentId: string; source: string }) {
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
  const field = 'w-full min-w-0 sm:w-40'

  return (
    <div className="space-y-3 rounded-lg border border-border bg-muted/20 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Input value={table} onChange={(e) => setTable(e.target.value)} placeholder="Table name" aria-label="Table name" className={field} />
        <FieldHelp helpKey="knowledge.datastore.table" route="knowledge" />
        <Select value={op} onValueChange={(v) => setOp(v as DatastoreOp)}>
          <SelectTrigger className="w-full sm:w-56" aria-label="What to look up">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {LOOKUP_OPERATIONS.map((o) => (
              <SelectItem key={o.value} value={o.value}>
                {o.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <FieldHelp helpKey="knowledge.datastore.operation" route="knowledge" />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {op === 'get_record' && (
          <>
            <Input value={pkValue} onChange={(e) => setPkValue(e.target.value)} placeholder="Record id" aria-label="Record id" className={field} />
            <FieldHelp helpKey="knowledge.datastore.primary_key" route="knowledge" />
          </>
        )}
        {op === 'find' && (
          <>
            <Input value={column} onChange={(e) => setColumn(e.target.value)} placeholder="Column" aria-label="Column" className={field} />
            <FieldHelp helpKey="knowledge.datastore.column" route="knowledge" />
            <Input value={value} onChange={(e) => setValue(e.target.value)} placeholder="Value to find" aria-label="Value to find" className={field} />
            <FieldHelp helpKey="knowledge.datastore.value" route="knowledge" />
          </>
        )}
        {op !== 'get_record' && (
          <>
            <Input value={limit} onChange={(e) => setLimit(e.target.value)} placeholder="How many" aria-label="How many" className="w-full min-w-0 sm:w-28" />
            <FieldHelp helpKey="knowledge.datastore.limit" route="knowledge" />
          </>
        )}
      </div>
      {!table ? (
        <p className="text-xs text-muted-foreground">Enter a table name to read from this datastore.</p>
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

/** The picked datastore: its tables and schema, the chunks it has indexed, and a
 *  way to look up one record. Schema is read from the persisted ontology, so it
 *  works with the backing database unreachable; chunk search degrades loudly. */
function DatastoreBrowse({ agentId, source }: { agentId: string; source: string }) {
  const [q, setQ] = useState('')
  const [mode, setMode] = useState<ChunkSearchMode>('literal')
  const tables = useConnectionTables(agentId, source)
  const browse = useConnectionChunks(agentId, source)
  const search = useConnectionChunkSearch(agentId, source, q, mode)
  const searching = q.trim().length > 0

  return (
    <div className="space-y-6">
      <section className="space-y-2">
        <h3 className={SECTION_HEADING}>Tables</h3>
        <QueryState
          query={tables}
          isEmpty={(d) => d.items.length === 0}
          empty={<p className="text-xs text-muted-foreground">This datastore exposes no tables yet.</p>}
        >
          {(data) => <EntityTable items={data.items} typeLabel="Type" />}
        </QueryState>
      </section>

      <section className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className={SECTION_HEADING}>Indexed chunks</h3>
          <Input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search chunks…"
            aria-label="Search chunks"
            className="w-full max-w-sm"
          />
          <FieldHelp helpKey="knowledge.chunk_search" route="knowledge" />
          <div className="flex gap-1">
            {(['literal', 'vector'] as ChunkSearchMode[]).map((m) => (
              <Button key={m} size="sm" variant={mode === m ? 'default' : 'outline'} aria-pressed={mode === m} onClick={() => setMode(m)}>
                {m === 'literal' ? 'Exact words' : 'By meaning'}
              </Button>
            ))}
          </div>
        </div>
        {searching && search.data?.degraded && (
          <p className="flex items-center gap-1.5 text-xs text-amber-600">
            <TriangleAlert className="size-3.5" />
            Search by meaning is unavailable for this agent, so these are exact-word results.
          </p>
        )}
        <QueryState<ChunkPage | ChunkSearchResponse>
          query={searching ? search : browse}
          isEmpty={(d) => d.items.length === 0}
          empty={
            <p className="text-xs text-muted-foreground">
              {searching ? 'No matching chunks.' : 'Nothing is indexed from this datastore yet. Run a sync from the Sources tab.'}
            </p>
          }
        >
          {(data) => (
            <ul className="space-y-2">
              {data.items.map((c) => (
                <li key={c.chunk_id} className="min-w-0 space-y-1.5 rounded-lg border border-border bg-muted/20 px-3 py-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <MonoChip>{c.source || c.chunk_id}</MonoChip>
                    <Chip>{c.classification}</Chip>
                    {c.truncated && <Chip>truncated</Chip>}
                  </div>
                  <p className="whitespace-pre-wrap break-words text-sm text-foreground">{c.text}</p>
                </li>
              ))}
            </ul>
          )}
        </QueryState>
      </section>

      <section className="space-y-2">
        <h3 className={SECTION_HEADING}>Look up a record</h3>
        <RecordLookup agentId={agentId} source={source} />
      </section>
    </div>
  )
}

// --- Browse: advanced -------------------------------------------------------

function TraceItem({ agentId }: { agentId: string }) {
  const [itemId, setItemId] = useState('')
  const trimmed = itemId.trim()
  const prov = useProvenance(agentId, trimmed || null)

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Input
          value={itemId}
          onChange={(e) => setItemId(e.target.value)}
          placeholder="Item id…"
          aria-label="Item id"
          className="w-full max-w-md"
        />
        <FieldHelp helpKey="knowledge.provenance.item_id" route="knowledge" />
      </div>
      {!trimmed ? (
        <p className="text-xs text-muted-foreground">Type an item id to see every source that claims it.</p>
      ) : (
        <QueryState
          query={prov}
          isEmpty={(d) => d.items.length === 0}
          empty={<p className="text-xs text-muted-foreground">No sources are recorded for this item.</p>}
        >
          {(data) => (
            <div className="overflow-x-auto rounded-lg border border-border bg-card shadow-xs">
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
                      <td className="px-3 py-2 align-top text-xs text-muted-foreground">{p.classification}</td>
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

function AdvancedBrowse({ agentId }: { agentId: string }) {
  return (
    <details className="rounded-lg border border-border bg-card/40 px-3 py-2">
      <summary className="cursor-pointer text-sm font-medium text-foreground">Advanced</summary>
      <div className="mt-3 space-y-2">
        <h3 className={SECTION_HEADING}>Trace an item id</h3>
        <TraceItem agentId={agentId} />
      </div>
    </details>
  )
}

/** One source picker, then the view that fits what the source is. */
function BrowseSection({ agentId }: { agentId: string }) {
  const sources = useConnectedSources(agentId)
  const [source, setSource] = useState('')
  const picked = (sources.data?.items ?? []).find((s) => s.source_id === source)

  return (
    <div className="space-y-6">
      <div className="space-y-3">
        <SourceSelect agentId={agentId} value={source} onChange={setSource} placeholder="Pick a source" />
        {!source ? (
          <EmptyState
            icon={<Library className="size-5" />}
            title="Pick a source"
            description="Choose a connected source to see its folders, documents or tables."
          />
        ) : isDatastoreKind(picked?.source_kind ?? '') ? (
          <DatastoreBrowse key={source} agentId={agentId} source={source} />
        ) : (
          <FileSourceBrowse key={source} agentId={agentId} source={source} />
        )}
      </div>
      <AdvancedBrowse agentId={agentId} />
    </div>
  )
}

// --- Browser shell ----------------------------------------------------------

/** The Connections offshoot of Knowledge (SPEC-073): an agent's connected data
 *  sources and their health, a browser that fits each source's kind, and the
 *  guides agents use to find their way around each connection. */
export function ConnectionsBrowser({
  agentId,
  initialConnectionId,
}: {
  agentId: string
  initialConnectionId?: string | null
}) {
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
        <SyncWorkerStatusLine />
        <SourcesSection agentId={agentId} initialConnectionId={initialConnectionId} />
      </TabsContent>
      <TabsContent value="browse">
        <BrowseSection agentId={agentId} />
      </TabsContent>
      <TabsContent value="guides">
        <GuidesSection agentId={agentId} />
      </TabsContent>
    </Tabs>
  )
}
