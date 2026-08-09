import { useState } from 'react'
import {
  Cable,
  LogIn,
  Plug,
  Stethoscope,
  RefreshCw,
  ShieldCheck,
  Trash2,
  TriangleAlert,
} from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { OperatorModeToggle } from '@/components/operator-mode-toggle'
import { AgentGrantChips } from '@/components/connection-grants'
import { ConnectorAuthorizePanel } from '@/components/connector-authorize-panel'
import { ConnectorSecretsSheet } from '@/components/connector-secrets-sheet'
import { HostRequirementLine } from '@/components/host-setup-panel'
import { Button } from '@/components/ui/button'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
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
import type { Agent, CatalogBundle, ConnectorInstance } from '@/lib/types'
import { cn } from '@/lib/utils'

// `status` is whatever `arc connector doctor` prints, so this reads the row's
// verdict rather than switching on a closed set the CLI does not promise.
function doctorTone(status: string): string {
  const s = status.toLowerCase()
  if (s.includes('ok') || s.includes('pass')) {
    return 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
  }
  if (s.includes('fail') || s.includes('error') || s.includes('missing')) {
    return 'border-destructive/30 bg-destructive/10 text-destructive'
  }
  return 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-400'
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

function ConnectionRow({
  inst,
  bundle,
  agents,
  operatorMode,
  onReauth,
}: {
  inst: ConnectorInstance
  /** The catalog entry backing this instance, absent if the bundle has left
   *  the extension search path. */
  bundle: CatalogBundle | undefined
  /** The whole fleet, so the row can show who does NOT hold this as well. */
  agents: Agent[]
  operatorMode: boolean
  onReauth: (bundle: CatalogBundle, instance: string) => void
}) {
  const probe = useProbeConnector(inst.instance)
  const approve = useApproveConnector(inst.instance)
  const remove = useRemoveConnector()
  const [showDoctor, setShowDoctor] = useState(false)
  const [showAuth, setShowAuth] = useState(false)
  const [confirmRemove, setConfirmRemove] = useState(false)

  const busy = probe.isPending || approve.isPending || remove.isPending
  // No declared secrets means the host binary holds the credential: there is
  // nothing to type, so this row signs in rather than opening a blank form.
  const holdsOwnLogin = bundle !== undefined && bundle.secrets.length === 0

  return (
    <>
      <TableRow>
        <TableCell className="font-medium text-foreground">{inst.instance}</TableCell>
        <TableCell>
          <span className="rounded-sm border border-border bg-muted/40 px-1.5 py-0.5 font-mono text-[11px] text-foreground">
            {inst.extension}
          </span>
        </TableCell>
        <TableCell className="min-w-64">
          <AgentGrantChips
            instance={inst.instance}
            agents={agents}
            holders={inst.agents}
            operatorMode={operatorMode}
          />
        </TableCell>
        <TableCell className="min-w-48">
          {probe.isPending ? (
            <span className="text-xs text-muted-foreground">Checking…</span>
          ) : probe.isError ? (
            <span className="text-xs text-destructive">{probe.error.message}</span>
          ) : probe.data ? (
            <span
              className={cn(
                'text-xs',
                probe.data.reachable
                  ? 'text-emerald-600 dark:text-emerald-400'
                  : 'text-destructive',
              )}
            >
              {probe.data.reachable ? 'Reachable' : 'Unreachable'}
              {probe.data.detail && (
                <span className="text-muted-foreground"> — {probe.data.detail}</span>
              )}
            </span>
          ) : (
            <span className="text-xs text-muted-foreground">Not checked</span>
          )}
        </TableCell>
        <TableCell>
          <div className="flex flex-wrap items-center gap-1.5">
            <Button variant="ghost" size="xs" onClick={() => setShowDoctor(!showDoctor)}>
              <Stethoscope /> {showDoctor ? 'Hide doctor' : 'Doctor'}
            </Button>
            {operatorMode && (
              <>
                <Button
                  variant="outline"
                  size="xs"
                  disabled={busy}
                  onClick={() => probe.mutate()}
                  title="Open a live connection and report what it serves"
                >
                  <RefreshCw /> Probe
                </Button>
                <Button
                  variant="outline"
                  size="xs"
                  disabled={busy}
                  onClick={() => approve.mutate()}
                  title="Record the tool contract this connection serves right now"
                >
                  <ShieldCheck /> Approve
                </Button>
                {holdsOwnLogin ? (
                  <Button
                    variant="outline"
                    size="xs"
                    onClick={() => setShowAuth(!showAuth)}
                    title={`Check or renew the ${inst.extension} sign-in on this computer`}
                  >
                    <LogIn /> {showAuth ? 'Hide sign-in' : 'Sign in'}
                  </Button>
                ) : (
                  <Button
                    variant="outline"
                    size="xs"
                    disabled={busy || !bundle}
                    onClick={() => bundle && onReauth(bundle, inst.instance)}
                    title={
                      bundle
                        ? 'Replace this connection’s credentials'
                        : `${inst.extension} is no longer on the extension search path`
                    }
                  >
                    Re-auth
                  </Button>
                )}
                {confirmRemove ? (
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
                ) : (
                  <Button
                    variant="ghost"
                    size="xs"
                    disabled={busy}
                    onClick={() => setConfirmRemove(true)}
                    className="text-destructive hover:text-destructive"
                    title="Remove this connection and its stored credentials"
                  >
                    <Trash2 /> Remove
                  </Button>
                )}
              </>
            )}
          </div>
          {approve.isError && (
            <p className="mt-1 text-xs text-destructive">{approve.error.message}</p>
          )}
          {approve.data && (
            <p className="mt-1 text-xs text-muted-foreground">
              Approved {approve.data.approved.length} tool
              {approve.data.approved.length === 1 ? '' : 's'}.
            </p>
          )}
          {remove.isError && (
            <p className="mt-1 text-xs text-destructive">{remove.error.message}</p>
          )}
        </TableCell>
      </TableRow>
      {showAuth && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={5} className="p-3">
            <ConnectorAuthorizePanel
              instance={inst.instance}
              extension={inst.extension}
              operatorMode={operatorMode}
            />
          </TableCell>
        </TableRow>
      )}
      {showDoctor && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={5} className="p-3">
            <DoctorPanel instance={inst.instance} />
          </TableCell>
        </TableRow>
      )}
    </>
  )
}

function BundleCard({
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
  return (
    // The bundle's directory is on the card as a hover title rather than a line
    // of its own. It matters exactly twice — telling two same-named bundles
    // apart, and working out why an expected one is missing — and neither is a
    // reason to put an absolute path in front of an operator who is choosing
    // what to connect.
    <div
      title={`from ${bundle.root}`}
      className="flex flex-col rounded-lg border border-border bg-card p-4 shadow-xs"
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="font-semibold text-foreground">{bundle.name}</span>
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
          <span className="rounded-sm border border-emerald-500/30 bg-emerald-500/10 px-1.5 py-0.5 text-[11px] font-medium text-emerald-700 dark:text-emerald-400">
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
          <Button size="sm" onClick={() => onConnect(bundle)}>
            <Plug /> Connect
          </Button>
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

  const catalog = useConnectorCatalog()
  const connections = useConnections()

  // One sheet drives both flows: no instance = install, instance = rotation.
  const [sheet, setSheet] = useState<{
    bundle: CatalogBundle
    instance?: string
  } | null>(null)

  const bundles = catalog.data?.available ?? []
  const instances = connections.data?.connections ?? []
  const bundleFor = (name: string) => bundles.find((b) => b.name === name)

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Connections"
        description="Every connected account on this computer, and which agents can use each one. Credentials are stored once and never displayed here."
        actions={<OperatorModeToggle />}
      />
      <div className="flex-1 space-y-8 overflow-auto p-6">
        <section className="space-y-3">
          {/* No search path here. Where bundles are read from is a fact about
              bundles, not about a connected account, and it belongs beside the
              list it explains. */}
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Connected
          </h2>
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
              <div className="rounded-lg border border-border bg-card">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Connection</TableHead>
                      <TableHead>Extension</TableHead>
                      <TableHead>Who can use it</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Actions</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.connections.map((inst) => (
                      <ConnectionRow
                        key={inst.instance}
                        inst={inst}
                        bundle={bundleFor(inst.extension)}
                        agents={agents}
                        operatorMode={operatorMode}
                        onReauth={(bundle, instance) => setSheet({ bundle, instance })}
                      />
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </QueryState>
        </section>

        <section className="space-y-3">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Available
          </h2>
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
                  <div className="space-y-1 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
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
