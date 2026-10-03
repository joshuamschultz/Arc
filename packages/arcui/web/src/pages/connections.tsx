import { useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  Cable,
  LogIn,
  Plug,
  Stethoscope,
  RefreshCw,
  ShieldCheck,
  Trash2,
  TriangleAlert,
  BookOpen,
  ChevronDown,
} from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { FieldHelp } from '@/components/help'
import { OperatorModeToggle } from '@/components/operator-mode-toggle'
import { ContextNote } from '@/components/hitl'
import { AgentGrantChips } from '@/components/connection-grants'
import { ConnectorAuthorizePanel } from '@/components/connector-authorize-panel'
import { OAuthConnectPanel } from '@/components/oauth-connect-panel'
import { ConnectorSecretsSheet } from '@/components/connector-secrets-sheet'
import { AddMcpServerDialog } from '@/components/add-mcp-server-dialog'
import { HostRequirementLine } from '@/components/host-setup-panel'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { QueryState, EmptyState } from '@/components/states'
import { useOperatorMode } from '@/hooks/use-operator-mode'
import {
  useApproveConnector,
  useConnections,
  useConnectorCatalog,
  useConnectorDoctor,
  useProbeConnector,
  useRemoveConnector,
  useRoster,
} from '@/lib/queries'
import type {
  Agent,
  CatalogBundle,
  ConnectionAction,
  ConnectionDisplayStatus,
  ConnectionKnowledgeSync,
  ConnectionNotice,
  ConnectorInstance,
} from '@/lib/types'
import { fmtTime, relativeTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import { agentLabel, grantName } from '@/lib/agent-names'

// `status` is whatever `arc connector doctor` prints, so this reads the row's
// verdict rather than switching on a closed set the CLI does not promise.
function doctorTone(status: string): string {
  const s = status.toLowerCase()
  if (s.includes('ok') || s.includes('pass')) {
    return 'border-status-online/30 bg-status-online/12 text-status-online'
  }
  if (s.includes('fail') || s.includes('error') || s.includes('missing')) {
    return 'border-status-error/30 bg-status-error/12 text-status-error'
  }
  return 'border-status-warning/30 bg-status-warning/12 text-status-warning'
}

// A small uppercase heading with the item count beside it, used to head each
// connection group. Plain word first, technical detail never.
function SectionHeading({ label, count }: { label: string; count?: number }) {
  return (
    <div className="flex items-center gap-2">
      <h2 className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
        {label}
      </h2>
      {count != null && count > 0 && (
        <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold tabular-nums text-muted-foreground">
          {count}
        </span>
      )}
    </div>
  )
}

// "Why is this one not working?" — the doctor rows exactly as the route
// returns them, opened under the connection they belong to.
function DoctorPanel({ instance }: { instance: string }) {
  const doctor = useConnectorDoctor(instance, true)
  return (
    <div className="rounded-md border border-border bg-muted/20 p-3">
      <QueryState
        query={doctor}
        isEmpty={(data) => data.checks.length === 0}
        empty={
          <EmptyState
            icon={<Stethoscope className="size-7" />}
            title="No checks"
            description="The doctor returned no rows for this connection."
          />
        }
      >
        {(data) => (
          <div className="space-y-1.5">
            {data.checks.map((c) => (
              <div key={c.check} className="flex items-start gap-2 text-xs">
                <span
                  className={cn(
                    'shrink-0 rounded-sm border px-1.5 py-0.5 text-[11px] font-medium',
                    doctorTone(c.status),
                  )}
                >
                  {c.status}
                </span>
                <span className="shrink-0 font-mono text-[11px] text-foreground">{c.check}</span>
                <span className="min-w-0 text-muted-foreground">{c.detail}</span>
              </div>
            ))}
          </div>
        )}
      </QueryState>
    </div>
  )
}

/**
 * Where bundles were read from, under the list it explains — one quiet line.
 *
 * An operator reads this for exactly one reason: a bundle they expected is not
 * on the page. So the ordinary case names the single directory to go and look
 * in, and the rare multi-root case says how many places were searched rather
 * than printing a row of absolute paths nobody scans — the full list is on
 * hover, where it costs nothing to carry.
 */
function SearchPathLine({ roots }: { roots: string[] }) {
  if (roots.length === 0) return null
  return (
    <p className="font-mono text-[11px] text-muted-foreground/70" title={roots.join('\n')}>
      {roots.length === 1 ? `read from ${roots[0]}` : `read from ${roots.length} locations`}
    </p>
  )
}

const STATUS_TONE: Record<ConnectionDisplayStatus, string> = {
  healthy: 'border-status-online/30 bg-status-online/12 text-status-online',
  needs_you: 'border-status-warning/30 bg-status-warning/12 text-status-warning',
  syncing: 'border-status-online/30 bg-status-online/12 text-status-online',
  error: 'border-status-error/30 bg-status-error/12 text-status-error',
  unknown: 'border-border bg-muted/40 text-muted-foreground',
}

const STATUS_LABEL: Record<ConnectionDisplayStatus, string> = {
  healthy: 'Healthy',
  needs_you: 'Needs you',
  syncing: 'Syncing',
  error: 'Error',
  unknown: 'Not checked yet',
}

// The one line under the chip, built only from the stored health row.
function statusLine(inst: ConnectorInstance): string {
  switch (inst.display_status) {
    case 'healthy':
      return inst.last_checked_at ? `Healthy · checked ${relativeTime(inst.last_checked_at)}` : 'Healthy'
    case 'needs_you':
      return `Needs you: ${inst.reason_text ?? ''}`.trim()
    case 'error':
      return `Error: ${inst.reason_text ?? ''}`.trim()
    case 'syncing':
      return 'Syncing'
    default:
      return 'Not checked yet'
  }
}

function noticeLine(notice: ConnectionNotice): string {
  return notice.delivered
    ? `Told you on ${notice.channel} at ${fmtTime(notice.at)}`
    : notice.channel === 'undeliverable'
      ? 'Could not notify you: no agent is running'
      : 'Could not notify you'
}

function ConnectionStatusChip({ status }: { status: ConnectionDisplayStatus }) {
  return (
    <span
      data-status-chip={status}
      className={cn(
        'rounded-sm border px-1.5 py-0.5 text-[11px] font-medium',
        STATUS_TONE[status],
      )}
    >
      {STATUS_LABEL[status]}
    </span>
  )
}

type OpenPanel = 'auth' | 'doctor' | null

function ConnectionCard({
  inst,
  bundle,
  agents,
  operatorMode,
  focused,
  onReauth,
}: {
  inst: ConnectorInstance
  /** The catalog entry backing this instance, absent if the bundle has left
   *  the extension search path. */
  bundle: CatalogBundle | undefined
  /** The whole fleet, so the card can show who does NOT hold this as well. */
  agents: Agent[]
  operatorMode: boolean
  /** Deep-linked from a notice: scroll into view and outline. */
  focused: boolean
  onReauth: (bundle: CatalogBundle, instance: string) => void
}) {
  const probe = useProbeConnector(inst.instance)
  const approve = useApproveConnector(inst.instance)
  const remove = useRemoveConnector()
  const [panel, setPanel] = useState<OpenPanel>(null)
  const [confirmRemove, setConfirmRemove] = useState(false)
  const cardRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (focused) cardRef.current?.scrollIntoView?.({ block: 'center', behavior: 'smooth' })
  }, [focused])

  const busy = probe.isPending || approve.isPending || remove.isPending
  // No declared secrets means the host binary holds the credential: there is
  // nothing to type, so there is no key form to open.
  const holdsOwnLogin = bundle !== undefined && bundle.secrets.length === 0
  const kind = inst.connect_kind
  const reauthLabel = kind === 'oauth' ? 'Edit details' : 'Re-auth'
  const togglePanel = (next: Exclude<OpenPanel, null>) =>
    setPanel((current) => (current === next ? null : next))

  const primaryClick: Partial<Record<ConnectionAction, () => void>> = {
    reconnect: () => togglePanel('auth'),
    approve: () => approve.mutate(),
    install_host: () => togglePanel('doctor'),
  }
  const onPrimary = primaryClick[inst.action]

  return (
    <div
      ref={cardRef}
      data-connection-card
      className={cn(
        'rounded-lg border border-border bg-card',
        focused && 'ring-2 ring-primary',
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-3 p-4">
        <div className="flex min-w-0 flex-col gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-display text-[15px] font-semibold text-foreground">
              {inst.instance}
            </span>
            <span
              title={inst.extension}
              className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] text-muted-foreground"
            >
              {inst.extension_display_name}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <ConnectionStatusChip status={inst.display_status} />
            <span data-status-line className="text-xs text-muted-foreground">
              {statusLine(inst)}
            </span>
          </div>
          {inst.last_notice && (
            <p className="text-[11px] text-muted-foreground">{noticeLine(inst.last_notice)}</p>
          )}
          {probe.isError && (
            <p className="text-[11px] text-destructive">{probe.error.message}</p>
          )}
        </div>
        {operatorMode && (
          <div className="flex items-center gap-1.5">
            {onPrimary && inst.action_label && (
              <Button size="xs" disabled={busy} onClick={onPrimary}>
                {inst.action_label}
              </Button>
            )}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="outline" size="xs">
                  Advanced <ChevronDown />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem disabled={busy} onSelect={() => probe.mutate()}>
                  <RefreshCw /> Check now
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => togglePanel('doctor')}>
                  <Stethoscope /> {panel === 'doctor' ? 'Hide doctor' : 'Doctor'}
                </DropdownMenuItem>
                {!holdsOwnLogin && (
                  <DropdownMenuItem
                    disabled={busy || !bundle}
                    onSelect={() => bundle && onReauth(bundle, inst.instance)}
                  >
                    <LogIn /> {reauthLabel}
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem disabled={busy} onSelect={() => approve.mutate()}>
                  <ShieldCheck /> Approve tools
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  className="text-destructive focus:text-destructive"
                  onSelect={() => setConfirmRemove(true)}
                >
                  <Trash2 /> Remove
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        )}
      </div>

      <div className="space-y-1.5 border-t border-border px-4 py-3">
        <p className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          Who can use it
        </p>
        <AgentGrantChips
          instance={inst.instance}
          agents={agents}
          holders={inst.agents}
          operatorMode={operatorMode}
        />
      </div>

      <div className="space-y-2 border-t border-border px-4 py-3">
        <p className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          Knowledge sync
        </p>
        {inst.knowledge_mode === 'non_indexable' ? (
          <p className="text-[11px] text-muted-foreground">
            Not indexable: {inst.knowledge_reason}
          </p>
        ) : inst.agents.length === 0 ? (
          <p className="text-[11px] text-muted-foreground">Grant this connection to an agent first.</p>
        ) : (
          <div className="space-y-1.5">
            {inst.knowledge_sync.map((row) => (
              <KnowledgeSyncRow key={`${row.agent}:${row.source_id}`} row={row} />
            ))}
            <div className="flex flex-wrap gap-1.5">
              {inst.agents.map((holder) => {
                const agent = agents.find((candidate) => grantName(candidate) === holder)
                return agent ? (
                  <ConnectionKnowledgeAction
                    key={holder}
                    agent={agent}
                    connectionId={inst.instance}
                  />
                ) : null
              })}
            </div>
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-1.5 border-t border-border px-4 py-3">
        <FieldHelp helpKey="connection.check" route="connections" />
        {operatorMode && confirmRemove && (
          <>
            <Button
              variant="destructive"
              size="xs"
              disabled={busy}
              onClick={() => remove.mutate(inst.instance)}
            >
              Confirm remove
            </Button>
            <Button variant="ghost" size="xs" onClick={() => setConfirmRemove(false)}>
              Cancel
            </Button>
          </>
        )}
      </div>

      {(approve.isError || approve.data || remove.isError) && (
        <div className="space-y-1 border-t border-border px-4 py-2">
          {approve.isError && (
            <p className="text-xs text-destructive">{approve.error.message}</p>
          )}
          {approve.data && (
            <p className="text-xs text-muted-foreground">
              Approved {approve.data.approved.length} tool
              {approve.data.approved.length === 1 ? '' : 's'}.
            </p>
          )}
          {remove.isError && (
            <p className="text-xs text-destructive">{remove.error.message}</p>
          )}
        </div>
      )}

      {operatorMode && panel === 'auth' && (
        <div className="border-t border-border p-4">
          {kind === 'oauth' ? (
            <OAuthConnectPanel
              instance={inst.instance}
              provider={inst.oauth_provider}
              reconnect={inst.action === 'reconnect'}
              onDone={() => setPanel(null)}
            />
          ) : (
            <ConnectorAuthorizePanel
              instance={inst.instance}
              extension={inst.extension}
              operatorMode={operatorMode}
            />
          )}
        </div>
      )}
      {panel === 'doctor' && (
        <div className="border-t border-border p-4">
          <DoctorPanel instance={inst.instance} />
        </div>
      )}
    </div>
  )
}

// One agent's sync of this connection, straight from the stored row.
function KnowledgeSyncRow({ row }: { row: ConnectionKnowledgeSync }) {
  return (
    <div data-knowledge-row className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground">
      <span className="font-medium text-foreground">{row.agent}</span>
      <span>Last sync: {row.last_synced_at ? relativeTime(row.last_synced_at) : 'Never'}</span>
      <span>{row.pages} pages</span>
      {row.error_code && <span className="text-destructive">{row.error_code}</span>}
      {row.running && <span className="font-medium text-status-online">Syncing</span>}
    </div>
  )
}

// Always navigates: whether the source is enrolled is the Knowledge page's
// question, not this card's, and asking here polled every agent every 10 s.
function ConnectionKnowledgeAction({
  agent,
  connectionId,
}: {
  agent: Agent
  connectionId: string
}) {
  const navigate = useNavigate()
  const agentId = agent.agent_id ?? grantName(agent)
  const label = agentLabel(agent)
  return (
    <Button
      variant="outline"
      size="xs"
      onClick={() =>
        navigate(
          `/knowledge?agent=${encodeURIComponent(agentId)}&tab=connections&connection=${encodeURIComponent(connectionId)}`,
        )
      }
      title={`Choose resources, approve mapping, and sync ${connectionId} for ${label}`}
    >
      <BookOpen />
      {`${label}: configure & sync`}
    </Button>
  )
}

export function BundleCard({
  bundle,
  connectedCount,
  operatorMode,
  onConnect,
}: {
  bundle: CatalogBundle
  connectedCount: number
  operatorMode: boolean
  onConnect: (bundle: CatalogBundle) => void
}) {
  // One bundle, many accounts: a bundle that asks for an account (Google) is added once
  // per address, so the button says what it does each time.
  const perAccount = bundle.secrets.some((s) => s.name === 'account')
  return (
    // The bundle's directory is on the card as a hover title rather than a line
    // of its own. It matters exactly twice — telling two same-named bundles
    // apart, and working out why an expected one is missing — and neither is a
    // reason to put an absolute path in front of an operator who is choosing
    // what to connect.
    <div
      title={`from ${bundle.root}`}
      className="flex flex-col rounded-lg border border-border bg-card p-4"
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="font-display text-[15px] font-semibold text-foreground" title={bundle.name}>
          {bundle.display_name}
        </span>
        <span className="font-mono text-[11px] text-muted-foreground">v{bundle.version}</span>
        <span className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] text-muted-foreground">
          {bundle.attachment}
        </span>
        <span className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] text-muted-foreground">
          {bundle.tier_floor}+
        </span>
        <span className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] text-muted-foreground">
          approval: {bundle.approval_default}
        </span>
        {connectedCount > 0 && (
          <span className="rounded-sm border border-status-online/30 bg-status-online/12 px-1.5 py-0.5 text-[11px] font-medium text-status-online">
            {connectedCount} connected
          </span>
        )}
      </div>
      <p className="mt-2 text-xs text-muted-foreground">{bundle.description}</p>
      <p className="mt-2 text-xs text-muted-foreground">
        {/* An empty `tools` means the manifest declares none, not that the
            bundle serves none — a cli bundle carries its verbs elsewhere and
            only the probe knows them. Never render that as "broken". */}
        {bundle.tools.length > 0
          ? `${bundle.tools.length} tool${bundle.tools.length === 1 ? '' : 's'} declared`
          : 'Tools resolved when you connect'}
        {bundle.secrets.length > 0 && (
          <>
            {' · asks for '}
            <span className="font-mono text-foreground">
              {bundle.secrets.map((s) => s.name).join(', ')}
            </span>
          </>
        )}
      </p>
      {bundle.host_requires.length > 0 && (
        <div className="mt-2">
          <HostRequirementLine requirements={bundle.host_requires} />
        </div>
      )}
      <div className="mt-3 pt-1">
        {operatorMode ? (
          <>
            <Button size="sm" onClick={() => onConnect(bundle)}>
              <Plug /> {perAccount ? 'Add an account' : 'Connect'}
            </Button>
            <FieldHelp helpKey="connection.bundle" route="connections" />
            {perAccount && (
              <p className="mt-1.5 text-[11px] text-muted-foreground">
                Each account is its own connection. After adding it, click the sign-in button on its card.
              </p>
            )}
          </>
        ) : (
          <p className="text-xs italic text-muted-foreground/80">
            Enable operator mode to connect this.
          </p>
        )}
      </div>
    </div>
  )
}

/**
 * Connections (SPEC-064). Answers, in order: what this deployment is connected
 * to and which agents hold each one, what else could be connected, and — per
 * connection — why one is not working.
 *
 * There is no agent selector, and that is the point. A connected account belongs
 * to the deployment; the agent-scoped thing is the GRANT, and the operator's
 * question was about several agents at once — *"maybe 2 agents can access jira
 * and 2 don't have the connection. maybe only 1 has gmail. and maybe all 4 have
 * confluence."* A per-agent page can only answer that by being visited four
 * times and the answers held in someone's head.
 *
 * Every mutating control is operator-only; the server is the real gate.
 */
export function ConnectionsPage() {
  const roster = useRoster()
  const agents = (roster.data?.agents ?? []).filter((a) => !a.hidden)
  const [operatorMode] = useOperatorMode()
  const [searchParams] = useSearchParams()
  const focusInstance = searchParams.get('connection')

  const catalog = useConnectorCatalog()
  const connections = useConnections()

  // One sheet drives both flows: no instance = install, instance = rotation.
  const [sheet, setSheet] = useState<{
    bundle: CatalogBundle
    instance?: string
  } | null>(null)

  // Its own dialog, opened from the page header: adding a server nobody shipped a bundle
  // for is not an action on any one card.
  const [addingMcp, setAddingMcp] = useState(false)

  const bundles = catalog.data?.available ?? []
  const instances = connections.data?.connections ?? []
  const bundleFor = (name: string) => bundles.find((b) => b.name === name)

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Connections"
        description="Every connected account on this computer, and which agents can use each one. Credentials are stored once and never displayed here."
        actions={
          <>
            {operatorMode && (
              <Button size="sm" variant="outline" onClick={() => setAddingMcp(true)}>
                <Plug /> Add MCP server
              </Button>
            )}
            <OperatorModeToggle />
          </>
        }
      />
      <div className="flex-1 space-y-8 overflow-auto p-6">
        <section className="space-y-3">
          {/* No search path here. Where bundles are read from is a fact about
              bundles, not about a connected account, and it belongs beside the
              list it explains. */}
          <SectionHeading label="Connected" count={instances.length} />
          <QueryState
            query={connections}
            isEmpty={(data) => data.connections.length === 0}
            empty={
              <EmptyState
                icon={<Cable className="size-7" />}
                title="No connections yet"
                description="Nothing is connected on this computer. Pick a bundle from Available below to connect one."
              />
            }
          >
            {(data) => (
              <div className="space-y-3">
                <ContextNote tone="info">
                  Each connection is one account this computer holds. The chips under{' '}
                  <span className="font-medium">Who can use it</span> decide which agents may reach
                  it — filled means yes, outlined means no.{' '}
                  {operatorMode
                    ? 'Click a chip to grant or revoke.'
                    : 'Turn on operator controls to grant or revoke.'}
                </ContextNote>
                {/* Denser 2-up grid once there is room; a single column below
                    `md` keeps every section (doctor, sign-in, knowledge sync)
                    readable rather than squeezed. `items-start` stops a tall
                    card from stretching its shorter neighbor to match height. */}
                <div className="grid grid-cols-1 items-start gap-3 md:grid-cols-2">
                  {data.connections.map((inst) => (
                    <ConnectionCard
                      key={inst.instance}
                      inst={inst}
                      bundle={bundleFor(inst.extension)}
                      agents={agents}
                      operatorMode={operatorMode}
                      focused={focusInstance === inst.instance}
                      onReauth={(bundle, instance) => setSheet({ bundle, instance })}
                    />
                  ))}
                </div>
              </div>
            )}
          </QueryState>
        </section>

        <section className="space-y-3">
          <SectionHeading label="Available" count={bundles.length} />
          <QueryState
            query={catalog}
            isEmpty={(data) => data.available.length === 0 && data.unreadable.length === 0}
            empty={
              <EmptyState
                icon={<Plug className="size-7" />}
                title="No bundles found"
                description="Nothing on the extension search path. Install a connector bundle to see it here."
              />
            }
          >
            {(data) => (
              <div className="space-y-3">
                <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
                  {data.available.map((b) => (
                    <BundleCard
                      key={b.name}
                      bundle={b}
                      connectedCount={instances.filter((i) => i.extension === b.name).length}
                      operatorMode={operatorMode}
                      onConnect={(bundle) => setSheet({ bundle })}
                    />
                  ))}
                </div>
                {data.unreadable.length > 0 && (
                  <div className="space-y-1 rounded-md border border-status-warning/30 bg-status-warning/12 px-3 py-2 text-xs text-status-warning">
                    <p className="flex items-center gap-2 font-medium">
                      <TriangleAlert className="size-4 shrink-0" />
                      Bundles found but not readable
                    </p>
                    {data.unreadable.map((u) => (
                      <p key={u.name}>
                        <span className="font-mono text-foreground">{u.name}</span> — {u.reason}
                      </p>
                    ))}
                  </div>
                )}
                <SearchPathLine roots={connections.data?.extensions_roots ?? []} />
              </div>
            )}
          </QueryState>
        </section>
      </div>
      {addingMcp && (
        <AddMcpServerDialog agents={agents} open onOpenChange={(o) => !o && setAddingMcp(false)} />
      )}
      {sheet && (
        <ConnectorSecretsSheet
          // Remount per target so no credential state survives a switch.
          key={`${sheet.bundle.name}:${sheet.instance ?? ''}`}
          bundle={sheet.bundle}
          instance={sheet.instance}
          agents={agents}
          open
          onOpenChange={(o) => !o && setSheet(null)}
        />
      )}
    </div>
  )
}
