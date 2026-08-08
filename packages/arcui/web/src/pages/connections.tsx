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
import { ConnectorAuthorizePanel } from '@/components/connector-authorize-panel'
import { ConnectorSecretsSheet } from '@/components/connector-secrets-sheet'
import { HostRequirementLine } from '@/components/host-setup-panel'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
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
  useAgentConnectors,
  useApproveConnector,
  useConnectorCatalog,
  useConnectorDoctor,
  useProbeConnector,
  useRemoveConnector,
  useRoster,
} from '@/lib/queries'
import type { CatalogBundle, ConnectorInstance } from '@/lib/types'
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
function DoctorPanel({ agentId, instance }: { agentId: string; instance: string }) {
  const doctor = useConnectorDoctor(agentId, instance, true)
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

function ConnectionRow({
  agentId,
  inst,
  bundle,
  operatorMode,
  onReauth,
}: {
  agentId: string
  inst: ConnectorInstance
  /** The catalog entry backing this instance, absent if the bundle has left
   *  the extension search path. */
  bundle: CatalogBundle | undefined
  operatorMode: boolean
  onReauth: (bundle: CatalogBundle, instance: string) => void
}) {
  const probe = useProbeConnector(agentId, inst.instance)
  const approve = useApproveConnector(agentId, inst.instance)
  const remove = useRemoveConnector(agentId)
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
        <TableCell className="text-xs text-muted-foreground">{inst.approval}</TableCell>
        <TableCell className="min-w-48">
          {probe.isPending ? (
            <span className="text-xs text-muted-foreground">Checking…</span>
          ) : probe.isError ? (
            <span className="text-xs text-destructive">{probe.error.message}</span>
          ) : probe.data ? (
            <span
              className={cn(
                'text-xs',
                probe.data.reachable ? 'text-emerald-600 dark:text-emerald-400' : 'text-destructive',
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
          {remove.isError && <p className="mt-1 text-xs text-destructive">{remove.error.message}</p>}
        </TableCell>
      </TableRow>
      {showAuth && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={5} className="p-3">
            <ConnectorAuthorizePanel
              agentId={agentId}
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
            <DoctorPanel agentId={agentId} instance={inst.instance} />
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
    <div className="flex flex-col rounded-lg border border-border bg-card p-4 shadow-xs">
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
      <p className="mt-1 font-mono text-[11px] text-muted-foreground/70">from {bundle.root}</p>
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
 * Connections (SPEC-064). Answers, in order: what is connected for this agent,
 * what could be connected, and — per connection — why one is not working.
 * Every mutating control is operator-only; the server is the real gate.
 */
export function ConnectionsPage() {
  const roster = useRoster()
  const agents = (roster.data?.agents ?? []).filter((a) => !a.hidden)
  const [picked, setPicked] = useState<string | null>(null)
  const agentId = picked ?? agents[0]?.agent_id ?? null
  const [operatorMode] = useOperatorMode()

  const catalog = useConnectorCatalog()
  const connectors = useAgentConnectors(agentId)

  // One sheet drives both flows: no instance = install, instance = rotation.
  const [sheet, setSheet] = useState<{ bundle: CatalogBundle; instance?: string } | null>(null)

  const bundles = catalog.data?.available ?? []
  const instances = connectors.data?.instances ?? []
  const bundleFor = (name: string) => bundles.find((b) => b.name === name)
  const currentAgent = agents.find((a) => a.agent_id === agentId)
  const agentName =
    currentAgent?.display_name || currentAgent?.name || currentAgent?.agent_id || 'this agent'

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Connections"
        description={`What ${agentName} is connected to, and what it could connect. Credentials are written straight to the agent; this surface never displays one.`}
        actions={
          <>
            <OperatorModeToggle />
            <Select value={agentId ?? ''} onValueChange={setPicked}>
              <SelectTrigger className="w-52">
                <SelectValue placeholder="Select agent" />
              </SelectTrigger>
              <SelectContent>
                {agents.map((a) => (
                  <SelectItem key={a.agent_id} value={a.agent_id ?? ''}>
                    {a.display_name || a.name || a.agent_id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </>
        }
      />
      {!agentId ? (
        <div className="flex-1 overflow-auto p-6">
          <EmptyState
            title="No agent selected"
            description="Pick an agent from the selector to see its connections."
          />
        </div>
      ) : (
        <div className="flex-1 space-y-8 overflow-auto p-6">
          <section className="space-y-3">
            <div>
              <h2 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                Connected
              </h2>
              {/* The agent-local root only. The search path is ordered and
                  plural (D-584); a bundle's own root shows on its card. */}
              {connectors.data?.extensions_root && (
                <p className="mt-0.5 text-[11px] text-muted-foreground/70">
                  agent-local root{' '}
                  <span className="font-mono">{connectors.data.extensions_root}</span>
                </p>
              )}
            </div>
            <QueryState
              query={connectors}
              isEmpty={(data) => data.instances.length === 0}
              empty={
                <EmptyState
                  icon={<Cable className="size-7" />}
                  title="No connections yet"
                  description={`${agentName} is not connected to anything. Pick a bundle from Available below to connect one.`}
                />
              }
            >
              {(data) => (
                <div className="rounded-lg border border-border bg-card">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Instance</TableHead>
                        <TableHead>Extension</TableHead>
                        <TableHead>Approval</TableHead>
                        <TableHead>Status</TableHead>
                        <TableHead>Actions</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {data.instances.map((inst) => (
                        <ConnectionRow
                          key={inst.instance}
                          agentId={agentId}
                          inst={inst}
                          bundle={bundleFor(inst.extension)}
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
                </div>
              )}
            </QueryState>
          </section>
        </div>
      )}
      {sheet && agentId && (
        <ConnectorSecretsSheet
          // Remount per target so no credential state survives a switch.
          key={`${sheet.bundle.name}:${sheet.instance ?? ''}`}
          agentId={agentId}
          bundle={sheet.bundle}
          instance={sheet.instance}
          open
          onOpenChange={(o) => !o && setSheet(null)}
        />
      )}
    </div>
  )
}
